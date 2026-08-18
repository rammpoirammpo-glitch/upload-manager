package tg

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"go.etcd.io/bbolt"
	"go.uber.org/zap"
	"github.com/skip2/go-qrcode"

	"github.com/gotd/td/telegram"
	"github.com/gotd/td/telegram/auth"
	"github.com/gotd/td/telegram/auth/qrlogin"
	"github.com/gotd/td/telegram/downloader"
	"github.com/gotd/td/telegram/uploader"
	"github.com/gotd/td/tg"
	"github.com/gotd/td/tgerr"
)

// Config is the app configuration an operator provides once.
type Config struct {
	APIID        int    `json:"api_id"`
	APIHash      string `json:"api_hash"`
	DownloadsDir string `json:"downloads_dir"`
}

// Self describes the logged-in account.
type Self struct {
	ID       int64  `json:"id"`
	Username string `json:"username"`
	First    string `json:"first_name"`
	Last     string `json:"last_name"`
	Phone    string `json:"phone"`
}

// Peer is a minimal chat overview used by the UI.
type Peer struct {
	ID    int64  `json:"id"`
	Type  string `json:"type"`
	Title string `json:"title"`
	Top   int    `json:"top_message"`
}

// MessageView is a chat message shown in the UI.
type MessageView struct {
	ID       int64  `json:"id"`
	Date     int64  `json:"date"`
	Text     string `json:"text"`
	HasMedia bool   `json:"has_media"`
	Media    string `json:"media,omitempty"`
	FileName string `json:"filename,omitempty"`
	FileSize int64  `json:"filesize,omitempty"`
}

// Transfer is an in-flight or finished download/upload for the UI.
type Transfer struct {
	ID        string  `json:"id"`
	Kind      string  `json:"kind"` // download | upload
	Name      string  `json:"name"`
	Total     int64   `json:"total"`
	Done      int64   `json:"done"`
	Percent   float64 `json:"percent"`
	Status    string  `json:"status"` // running | done | error
	Error     string  `json:"error,omitempty"`
	StartedAt int64   `json:"started_at"`
}

// AuthState reports configuration and login state.
type AuthState struct {
	Configured bool   `json:"configured"`
	Authorized bool   `json:"authorized"`
	Needs2FA   bool   `json:"needs_2fa"`
	Self       *Self  `json:"self,omitempty"`
	Status     string `json:"status"`
}

// Service wraps a gotd client plus HTTP-facing convenience methods.
type Service struct {
	mu       sync.Mutex
	db       *bbolt.DB
	dataDir  string
	cfg      Config
	client   *telegram.Client
	cancel   context.CancelFunc
	ready    chan struct{} // closed once the client is connected

	transfersMu sync.Mutex
	transfers   map[string]*Transfer

	qrToken   qrlogin.Token
	qrTokenMu sync.Mutex
}

var errNotConfigured = errors.New("telegram API_ID and API_HASH are not set")

var errNeedsPassword = errors.New("password needed")

func NewService(dataDir string) (*Service, error) {
	if err := os.MkdirAll(dataDir, 0o700); err != nil {
		return nil, err
	}
	db, err := bbolt.Open(filepath.Join(dataDir, "state.db"), 0o600, nil)
	if err != nil {
		return nil, err
	}
	s := &Service{
		db:        db,
		dataDir:   dataDir,
		ready:     make(chan struct{}),
		transfers: map[string]*Transfer{},
	}
	if err := s.loadConfig(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Service) Close() error {
	if s.cancel != nil {
		s.cancel()
	}
	return s.db.Close()
}

// ---- config ----

func (s *Service) configPath() string { return filepath.Join(s.dataDir, "config.json") }

func (s *Service) loadConfig() error {
	data, err := os.ReadFile(s.configPath())
	if err != nil {
		return nil
	}
	return json.Unmarshal(data, &s.cfg)
}

func (s *Service) saveConfig() error {
	data, err := json.Marshal(s.cfg)
	if err != nil {
		return err
	}
	return os.WriteFile(s.configPath(), data, 0o600)
}

func (s *Service) SetConfig(ctx context.Context, c Config) error {
	s.mu.Lock()
	if c.DownloadsDir == "" {
		c.DownloadsDir = filepath.Join(s.dataDir, "downloads")
	}
	s.cfg = c
	err := s.saveConfig()
	s.mu.Unlock()
	if err != nil {
		return err
	}
	return s.connect(ctx)
}

func (s *Service) GetConfig() Config {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cfg
}

func (s *Service) IsConfigured() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cfg.APIID != 0 && s.cfg.APIHash != ""
}

// ---- client lifecycle ----

func (s *Service) connect(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cfg.APIID == 0 || s.cfg.APIHash == "" {
		return errNotConfigured
	}
	if s.client != nil {
		return nil
	}

	client := telegram.NewClient(s.cfg.APIID, s.cfg.APIHash, telegram.Options{
		SessionStorage: &boltSession{db: s.db},
		Logger:         zap.NewNop(),
	})
	if s.cancel != nil {
		s.cancel()
	}
	runCtx, cancel := context.WithCancel(context.Background())
	s.cancel = cancel
	s.client = client

	go func() {
		_ = client.Run(runCtx, func(rctx context.Context) error {
			if err := client.Ping(rctx); err != nil {
				return err
			}
			closeSafeReady(s.ready)
			<-rctx.Done()
			return rctx.Err()
		})
	}()

	select {
	case <-s.ready:
		return nil
	case <-ctx.Done():
		s.mu.Lock()
		s.client = nil
		s.mu.Unlock()
		return ctx.Err()
	case <-time.After(25 * time.Second):
		s.mu.Lock()
		s.client = nil
		s.mu.Unlock()
		return errors.New("could not connect to Telegram (timeout)")
	}
}

func (s *Service) ensureReady() (*telegram.Client, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.client == nil {
		return nil, errors.New("client is not connected")
	}
	return s.client, nil
}

// CurrentStatus reports configuration and login state.
func (s *Service) CurrentStatus(ctx context.Context) AuthState {
	s.mu.Lock()
	st := AuthState{Configured: s.cfg.APIID != 0 && s.cfg.APIHash != ""}
	cl := s.client
	s.mu.Unlock()

	if !st.Configured {
		st.Status = "not_configured"
		return st
	}
	if cl == nil {
		st.Status = "connecting"
		return st
	}
	status, err := cl.Auth().Status(ctx)
	if err != nil {
		st.Status = "disconnected"
		return st
	}
	if !status.Authorized {
		st.Status = "unauthorized"
		return st
	}
	u, err := cl.Self(ctx)
	if err != nil {
		st.Status = "unauthorized"
		return st
	}
	st.Authorized = true
	st.Status = "authorized"
	st.Self = &Self{ID: u.ID, Username: u.Username, First: u.FirstName, Last: u.LastName, Phone: u.Phone}
	return st
}

// ---- login ----

// StartQRLogin exports a fresh QR token and returns its URL + expiry.
func (s *Service) StartQRLogin(ctx context.Context) (string, int64, error) {
	cl, err := s.ensureReady()
	if err != nil {
		return "", 0, err
	}
	qr := cl.QR()
	token, err := qr.Export(ctx)
	if err != nil {
		return "", 0, err
	}
	s.qrTokenMu.Lock()
	s.qrToken = token
	s.qrTokenMu.Unlock()
	return token.URL(), token.Expires().Unix(), nil
}

func (s *Service) QRTokenURL() string {
	s.qrTokenMu.Lock()
	defer s.qrTokenMu.Unlock()
	return s.qrToken.URL()
}

// QRPNG renders the current QR token as a PNG image.
func (s *Service) QRPNG() ([]byte, error) {
	url := s.QRTokenURL()
	if url == "" {
		return nil, errors.New("no pending QR token")
	}
	qr, err := qrcode.New(url, qrcode.Medium)
	if err != nil {
		return nil, err
	}
	return qr.PNG(280)
}

// BlockForQR waits until the QR token is accepted.
func (s *Service) BlockForQR(ctx context.Context) error {
	cl, err := s.ensureReady()
	if err != nil {
		return err
	}
	s.qrTokenMu.Lock()
	token := s.qrToken
	s.qrTokenMu.Unlock()
	if token.Empty() {
		return errors.New("no pending QR token")
	}
	_, err = cl.QR().Accept(ctx, token)
	if err != nil {
		if tgerr.Is(err, "SESSION_PASSWORD_NEEDED") {
			return errNeedsPassword
		}
		return err
	}
	return nil
}

// SubmitPassword completes a 2FA-protected login.
func (s *Service) SubmitPassword(ctx context.Context, password string) error {
	cl, err := s.ensureReady()
	if err != nil {
		return err
	}
	_, err = cl.Auth().Password(ctx, password)
	return err
}

// StartCodeLogin requests an SMS/login code for a phone number.
func (s *Service) StartCodeLogin(ctx context.Context, phone string) (string, error) {
	cl, err := s.ensureReady()
	if err != nil {
		return "", err
	}
	sent, err := cl.Auth().SendCode(ctx, phone, auth.SendCodeOptions{})
	if err != nil {
		return "", err
	}
	if c, ok := sent.(*tg.AuthSentCode); ok {
		return c.PhoneCodeHash, nil
	}
	return "", errors.New("unexpected reply to SendCode")
}

// SubmitCodeLogin signs in with the received code.
func (s *Service) SubmitCodeLogin(ctx context.Context, phone, hash, code string) error {
	cl, err := s.ensureReady()
	if err != nil {
		return err
	}
	_, err = cl.Auth().SignIn(ctx, phone, code, hash)
	if err != nil {
		if tgerr.Is(err, "SESSION_PASSWORD_NEEDED") {
			return errNeedsPassword
		}
		return err
	}
	return nil
}

// Logout terminates the Telegram session.
func (s *Service) Logout(ctx context.Context) error {
	cl, err := s.ensureReady()
	if err != nil {
		return err
	}
	_, err = cl.API().AuthLogOut(ctx)
	_ = s.db.Update(func(tx *bbolt.Tx) error {
		return tx.DeleteBucket(sessionBucket)
	})
	return err
}

// ---- browsing ----

func (s *Service) Dialogs(ctx context.Context) ([]Peer, error) {
	cl, err := s.ensureReady()
	if err != nil {
		return nil, err
	}
	res, err := cl.API().MessagesGetDialogs(ctx, &tg.MessagesGetDialogsRequest{
		OffsetPeer: &tg.InputPeerEmpty{},
		Limit:      200,
	})
	if err != nil {
		return nil, err
	}
	return dialogsToPeers(res)
}

func dialogsToPeers(res tg.MessagesDialogsClass) ([]Peer, error) {
	var dialogs []tg.DialogClass
	var users []tg.UserClass
	var chats []tg.ChatClass

	switch m := res.(type) {
	case *tg.MessagesDialogs:
		dialogs, users, chats = m.Dialogs, m.Users, m.Chats
	case *tg.MessagesDialogsSlice:
		dialogs, users, chats = m.Dialogs, m.Users, m.Chats
	default:
		return nil, fmt.Errorf("unexpected dialogs response: %T", res)
	}

	names := map[int64]string{}
	for _, u := range users {
		if t, ok := u.(*tg.User); ok {
			name := strings.TrimSpace(t.FirstName + " " + t.LastName)
			if name == "" {
				name = t.Username
			}
			if name == "" {
				name = fmt.Sprintf("user %d", t.ID)
			}
			names[t.ID] = name
		}
	}
	for _, c := range chats {
		switch c := c.(type) {
		case *tg.Channel:
			names[c.ID] = c.Title
		case *tg.Chat:
			names[c.ID] = c.Title
		}
	}

	out := make([]Peer, 0, len(dialogs))
	for _, d := range dialogs {
		dlg, ok := d.(*tg.Dialog)
		if !ok {
			continue
		}
		id := int64(0)
		typ := "?"
		switch p := dlg.Peer.(type) {
		case *tg.PeerChannel:
			id, typ = p.ChannelID, "channel"
		case *tg.PeerUser:
			id, typ = p.UserID, "user"
		case *tg.PeerChat:
			id, typ = p.ChatID, "group"
		}
		title := names[id]
		if title == "" {
			title = fmt.Sprintf("peer %d", id)
		}
		out = append(out, Peer{ID: id, Type: typ, Title: title, Top: dlg.TopMessage})
	}
	return out, nil
}

func (s *Service) Messages(ctx context.Context, peerID int64, offsetID int, limit int) ([]MessageView, error) {
	cl, err := s.ensureReady()
	if err != nil {
		return nil, err
	}
	ip, err := s.inputPeer(ctx, cl, peerID)
	if err != nil {
		return nil, err
	}
	if limit <= 0 || limit > 100 {
		limit = 50
	}
	res, err := cl.API().MessagesGetHistory(ctx, &tg.MessagesGetHistoryRequest{
		Peer:     ip,
		Limit:    limit,
		OffsetID: offsetID,
	})
	if err != nil {
		return nil, err
	}

	var out []MessageView
	for _, mc := range messagesClass(res) {
		m, ok := mc.(*tg.Message)
		if !ok {
			continue
		}
		v := MessageView{ID: int64(m.ID), Date: int64(m.Date), Text: m.Message}
		name, size, kind := mediaInfo(m)
		if kind != "" {
			v.HasMedia = true
			v.Media = kind
			v.FileName = name
			v.FileSize = size
		}
		out = append(out, v)
	}
	return out, nil
}

func messagesClass(res tg.MessagesMessagesClass) []tg.MessageClass {
	switch m := res.(type) {
	case *tg.MessagesMessages:
		return m.Messages
	case *tg.MessagesMessagesSlice:
		return m.Messages
	case *tg.MessagesChannelMessages:
		return m.Messages
	}
	return nil
}

func mediaInfo(m *tg.Message) (name string, size int64, kind string) {
	switch mm := m.Media.(type) {
	case *tg.MessageMediaDocument:
		d, ok := mm.GetDocument()
		doc, ok2 := d.(*tg.Document)
		if !ok || !ok2 {
			return
		}
		for _, a := range doc.Attributes {
			if fn, ok := a.(*tg.DocumentAttributeFilename); ok {
				name = fn.FileName
			}
		}
		if name == "" {
			name = fmt.Sprintf("%d", doc.ID)
		}
		size = doc.Size
		kind = "document"
	case *tg.MessageMediaPhoto:
		p, ok := mm.GetPhoto()
		if !ok {
			return
		}
		if ph, ok2 := p.(*tg.Photo); ok2 {
			name = fmt.Sprintf("%d.jpg", ph.ID)
		}
		kind = "photo"
	}
	return
}

// inputPeer resolves an ID into an InputPeer using access hashes from dialogs.
func (s *Service) inputPeer(ctx context.Context, cl *telegram.Client, id int64) (tg.InputPeerClass, error) {
	res, err := cl.API().MessagesGetDialogs(ctx, &tg.MessagesGetDialogsRequest{
		OffsetPeer: &tg.InputPeerEmpty{},
		Limit:      200,
	})
	if err != nil {
		return nil, err
	}
	users, chats := dialogsPeers(res)
	for _, u := range users {
		if t, ok := u.(*tg.User); ok && t.ID == id {
			return &tg.InputPeerUser{UserID: id, AccessHash: t.AccessHash}, nil
		}
	}
	for _, c := range chats {
		switch c := c.(type) {
		case *tg.Channel:
			if c.ID == id {
				return &tg.InputPeerChannel{ChannelID: id, AccessHash: c.AccessHash}, nil
			}
		case *tg.Chat:
			if c.ID == id {
				return &tg.InputPeerChat{ChatID: id}, nil
			}
		}
	}
	return nil, fmt.Errorf("peer %d not found in dialogs", id)
}

func dialogsPeers(res tg.MessagesDialogsClass) ([]tg.UserClass, []tg.ChatClass) {
	switch m := res.(type) {
	case *tg.MessagesDialogs:
		return m.Users, m.Chats
	case *tg.MessagesDialogsSlice:
		return m.Users, m.Chats
	}
	return nil, nil
}

// ---- download ----

func (s *Service) DownloadMedia(ctx context.Context, peerID int64, ids []int64) ([]string, error) {
	cl, err := s.ensureReady()
	if err != nil {
		return nil, err
	}
	msgs, err := s.fetchMessages(ctx, cl, peerID, ids)
	if err != nil {
		return nil, err
	}
	dir := s.cfg.DownloadsDir
	if dir == "" {
		dir = filepath.Join(s.dataDir, "downloads")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	var saved []string
	dl := downloader.NewDownloader()
	for _, m := range msgs {
		loc, _, err := fileLocation(m)
		if err != nil {
			continue
		}
		name := fmt.Sprintf("peer%d_%d", peerID, m.ID)
		size := int64(0)
		if fname, fsize, _ := mediaInfo(m); fname != "" {
			name = fmt.Sprintf("peer%d_%d_%s", peerID, m.ID, safeName(fname))
			size = fsize
		}
		dest := filepath.Join(dir, name)
		saved = append(saved, dest)

		t := &Transfer{ID: newID(), Kind: "download", Name: name, Status: "running", StartedAt: nowUnix()}
		s.setTransfer(t)

		f, err := os.Create(dest)
		if err != nil {
			return saved, err
		}
		w := &countingWriter{w: f, size: size, update: func(done, total int64) {
			t.Done = done
			t.Total = total
			if total > 0 {
				t.Percent = float64(done) / float64(total) * 100
			}
			s.setTransfer(t)
		}}
		b := dl.Download(cl.API(), copyLoc(loc))
		_, err = b.WithThreads(3).Stream(ctx, w)
		f.Close()
		if err != nil {
			t.Status = "error"
			t.Error = err.Error()
			s.setTransfer(t)
			return saved, err
		}
		t.Status = "done"
		t.Percent = 100
		s.setTransfer(t)
	}
	return saved, nil
}

// ---- upload ----

type chunkProgress struct{ update func(uploaded, total int64) }

func (c *chunkProgress) Chunk(_ context.Context, st uploader.ProgressState) error {
	c.update(st.Uploaded, st.Total)
	return nil
}

func (s *Service) UploadFile(ctx context.Context, to int64, path string, caption string) (string, error) {
	cl, err := s.ensureReady()
	if err != nil {
		return "", err
	}
	ip, err := s.inputPeer(ctx, cl, to)
	if err != nil {
		return "", err
	}
	st, err := os.Stat(path)
	if err != nil {
		return "", err
	}

	t := &Transfer{ID: newID(), Kind: "upload", Name: filepath.Base(path), Status: "running", StartedAt: nowUnix()}
	t.Total = st.Size()
	s.setTransfer(t)

	up := uploader.NewUploader(cl.API())
	up = up.WithProgress(&chunkProgress{update: func(uploaded, total int64) {
		t.Done = uploaded
		if total > 0 {
			t.Total = total
			t.Percent = float64(uploaded) / float64(total) * 100
		}
		s.setTransfer(t)
	}})

	file, err := up.FromPath(ctx, path)
	if err != nil {
		return "", err
	}

	mimeType := mime.TypeByExtension(filepath.Ext(path))
	if mimeType == "" {
		mimeType = "application/octet-stream"
	}
	media := &tg.InputMediaUploadedDocument{
		File:       file,
		MimeType:   mimeType,
		Attributes: []tg.DocumentAttributeClass{&tg.DocumentAttributeFilename{FileName: filepath.Base(path)}},
	}
	_, err = cl.API().MessagesSendMedia(ctx, &tg.MessagesSendMediaRequest{
		Peer:     ip,
		Media:    media,
		Message:  caption,
		RandomID: randInt64(),
	})
	if err != nil {
		t.Status = "error"
		t.Error = err.Error()
		s.setTransfer(t)
		return "", err
	}
	t.Status = "done"
	t.Percent = 100
	s.setTransfer(t)
	return "sent", nil
}

// ---- forward ----

func (s *Service) ForwardMessages(ctx context.Context, from int64, to int64, ids []int64) (string, error) {
	cl, err := s.ensureReady()
	if err != nil {
		return "", err
	}
	fromIP, err := s.inputPeer(ctx, cl, from)
	if err != nil {
		return "", err
	}
	toIP, err := s.inputPeer(ctx, cl, to)
	if err != nil {
		return "", err
	}
	rnd := make([]int64, len(ids))
	for i := range rnd {
		rnd[i] = randInt64()
	}
	idsInt := make([]int, len(ids))
	for i, id := range ids {
		idsInt[i] = int(id)
	}
	if _, err := cl.API().MessagesForwardMessages(ctx, &tg.MessagesForwardMessagesRequest{
		FromPeer: fromIP,
		ID:       idsInt,
		RandomID: rnd,
		ToPeer:   toIP,
	}); err != nil {
		return "", err
	}
	return "forwarded", nil
}

func (s *Service) fetchMessages(ctx context.Context, cl *telegram.Client, peerID int64, ids []int64) ([]*tg.Message, error) {
	ip, err := s.inputPeer(ctx, cl, peerID)
	if err != nil {
		return nil, err
	}
	in := make([]tg.InputMessageClass, 0, len(ids))
	for _, id := range ids {
		in = append(in, &tg.InputMessageID{ID: int(id)})
	}
	var res tg.MessagesMessagesClass
	switch p := ip.(type) {
	case *tg.InputPeerChannel:
		res, err = cl.API().ChannelsGetMessages(ctx, &tg.ChannelsGetMessagesRequest{
			Channel: &tg.InputChannel{ChannelID: p.ChannelID, AccessHash: p.AccessHash},
			ID:      in,
		})
	case *tg.InputPeerUser, *tg.InputPeerChat:
		res, err = cl.API().MessagesGetMessages(ctx, in)
	default:
		return nil, errors.New("unsupported peer for fetchMessages")
	}
	if err != nil {
		return nil, err
	}
	var out []*tg.Message
	for _, mc := range messagesClass(res) {
		if m, ok := mc.(*tg.Message); ok {
			out = append(out, m)
		}
	}
	return out, nil
}

// ---- transfers ----

func (s *Service) Transfers() []*Transfer {
	s.transfersMu.Lock()
	defer s.transfersMu.Unlock()
	out := make([]*Transfer, 0, len(s.transfers))
	for _, t := range s.transfers {
		out = append(out, t)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].StartedAt > out[j].StartedAt })
	return out
}

func (s *Service) setTransfer(t *Transfer) {
	s.transfersMu.Lock()
	s.transfers[t.ID] = t
	s.transfersMu.Unlock()
}

// ---- helpers ----

type countingWriter struct {
	w      io.Writer
	size   int64
	done   int64
	update func(done, total int64)
}

func (c *countingWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.done += int64(n)
	total := c.size
	if total == 0 {
		total = c.done
	}
	c.update(c.done, total)
	return n, err
}

func fileLocation(m *tg.Message) (tg.InputFileLocationClass, string, error) {
	switch mm := m.Media.(type) {
	case *tg.MessageMediaDocument:
		d, ok := mm.GetDocument()
		doc, ok2 := d.(*tg.Document)
		if !ok || !ok2 {
			return nil, "", errors.New("unsupported document")
		}
		return &tg.InputDocumentFileLocation{
			ID:            doc.ID,
			AccessHash:    doc.AccessHash,
			FileReference: doc.FileReference,
		}, fmt.Sprintf("%d", doc.ID), nil
	case *tg.MessageMediaPhoto:
		p, ok := mm.GetPhoto()
		ph, ok2 := p.(*tg.Photo)
		if !ok || !ok2 || len(ph.Sizes) == 0 {
			return nil, "", errors.New("unsupported photo")
		}
		biggest := ph.Sizes[0]
		for _, sz := range ph.Sizes[1:] {
			if photoSizeBytes(sz) > photoSizeBytes(biggest) {
				biggest = sz
			}
		}
		return &tg.InputPhotoFileLocation{
			ID:            ph.ID,
			AccessHash:    ph.AccessHash,
			FileReference: ph.FileReference,
			ThumbSize:     photoSizeType(biggest),
		}, fmt.Sprintf("%d.jpg", ph.ID), nil
	}
	return nil, "", errors.New("message has no downloadable media")
}

func photoSizeBytes(s tg.PhotoSizeClass) int64 {
	if ps, ok := s.(*tg.PhotoSize); ok {
		return int64(ps.Size)
	}
	return 0
}

func photoSizeType(s tg.PhotoSizeClass) string {
	if ps, ok := s.(*tg.PhotoSize); ok {
		return ps.Type
	}
	return "x"
}

func copyLoc(loc tg.InputFileLocationClass) tg.InputFileLocationClass {
	switch l := loc.(type) {
	case *tg.InputDocumentFileLocation:
		c := *l
		return &c
	case *tg.InputPhotoFileLocation:
		c := *l
		return &c
	}
	return loc
}

func safeName(n string) string {
	n = strings.Map(func(r rune) rune {
		if r == '/' || r == '\\' || r == 0 {
			return '_'
		}
		return r
	}, n)
	if n == "" || n == "." || n == ".." {
		return "file"
	}
	return n
}

func newID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return fmt.Sprintf("%x", b)
}

func randInt64() int64 {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return int64(binary.LittleEndian.Uint64(b[:]))
}

func nowUnix() int64 { return time.Now().Unix() }

func closeSafeReady(ch chan struct{}) {
	select {
	case <-ch:
	default:
		close(ch)
	}
}