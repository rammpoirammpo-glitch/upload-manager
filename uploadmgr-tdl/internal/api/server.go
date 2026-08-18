package api

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"tdlweb/internal/tg"
)

type Server struct {
	svc      *tg.Service
	staticFS http.FileSystem
}

func New(svc *tg.Service, staticFS http.FileSystem) *Server {
	return &Server{svc: svc, staticFS: staticFS}
}

func (s *Server) writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func (s *Server) writeErr(w http.ResponseWriter, code int, err error) {
	s.writeJSON(w, code, map[string]string{"error": err.Error()})
}

func (s *Server) startQRLogin(w http.ResponseWriter, r *http.Request) {
	_, expires, err := s.svc.StartQRLogin(r.Context())
	if err != nil {
		s.writeErr(w, 500, err)
		return
	}
	s.writeJSON(w, 200, map[string]any{"expires": expires})
}

func (s *Server) qrStatus(w http.ResponseWriter, r *http.Request) {
	s.writeJSON(w, 200, s.svc.RegisterQRStatus())
}

func (s *Server) qrPNG(w http.ResponseWriter, r *http.Request) {
	png, err := s.svc.QRPNG()
	if err != nil {
		s.writeErr(w, 500, err)
		return
	}
	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(200)
	_, _ = w.Write(png)
}

func (s *Server) waitQR(w http.ResponseWriter, r *http.Request) {
	s.writeJSON(w, 200, s.svc.RegisterQRStatus())
}

func (s *Server) status(w http.ResponseWriter, r *http.Request) {
	s.writeJSON(w, 200, s.svc.CurrentStatus(r.Context()))
}

func (s *Server) setConfig(w http.ResponseWriter, r *http.Request) {
	var body struct {
		APIID        int    `json:"api_id"`
		APIHash      string `json:"api_hash"`
		DownloadsDir string `json:"downloads_dir"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		s.writeErr(w, 400, err)
		return
	}
	if body.APIID == 0 || strings.TrimSpace(body.APIHash) == "" {
		s.writeErr(w, 400, fmt.Errorf("api_id and api_hash are required"))
		return
	}
	if err := s.svc.SetConfig(r.Context(), tg.Config{
		APIID:        body.APIID,
		APIHash:      strings.TrimSpace(body.APIHash),
		DownloadsDir: body.DownloadsDir,
	}); err != nil {
		s.writeErr(w, 500, err)
		return
	}
	s.writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) loginCode(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Phone string `json:"phone"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		s.writeErr(w, 400, err)
		return
	}
	hash, err := s.svc.StartCodeLogin(r.Context(), body.Phone)
	if err != nil {
		s.writeErr(w, 500, err)
		return
	}
	s.writeJSON(w, 200, map[string]any{"hash": hash})
}

func (s *Server) loginCodeSubmit(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Phone string `json:"phone"`
		Hash  string `json:"hash"`
		Code  string `json:"code"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		s.writeErr(w, 400, err)
		return
	}
	err := s.svc.SubmitCodeLogin(r.Context(), body.Phone, body.Hash, body.Code)
	if err != nil {
		if err.Error() == "password needed" {
			s.writeJSON(w, 200, map[string]any{"needs_2fa": true})
			return
		}
		s.writeErr(w, 500, err)
		return
	}
	s.writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) loginPassword(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Password string `json:"password"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		s.writeErr(w, 400, err)
		return
	}
	if err := s.svc.SubmitPassword(r.Context(), body.Password); err != nil {
		s.writeErr(w, 500, err)
		return
	}
	s.writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) logout(w http.ResponseWriter, r *http.Request) {
	if err := s.svc.Logout(r.Context()); err != nil {
		s.writeErr(w, 500, err)
		return
	}
	s.writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) dialogs(w http.ResponseWriter, r *http.Request) {
	peers, err := s.svc.Dialogs(r.Context())
	if err != nil {
		s.writeErr(w, 500, err)
		return
	}
	s.writeJSON(w, 200, map[string]any{"peers": peers})
}

func (s *Server) messages(w http.ResponseWriter, r *http.Request) {
	peerID, err := strconv.ParseInt(r.URL.Query().Get("peer"), 10, 64)
	if err != nil {
		s.writeErr(w, 400, fmt.Errorf("invalid peer"))
		return
	}
	offset, _ := strconv.Atoi(r.URL.Query().Get("offset"))
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	msgs, err := s.svc.Messages(r.Context(), peerID, offset, limit)
	if err != nil {
		s.writeErr(w, 500, err)
		return
	}
	s.writeJSON(w, 200, map[string]any{"messages": msgs})
}

func (s *Server) download(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Peer int64   `json:"peer"`
		IDs  []int64 `json:"ids"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		s.writeErr(w, 400, err)
		return
	}
	if body.Peer == 0 || len(body.IDs) == 0 {
		s.writeErr(w, 400, fmt.Errorf("peer and ids required"))
		return
	}
	paths, err := s.svc.DownloadMedia(r.Context(), body.Peer, body.IDs)
	if err != nil {
		s.writeErr(w, 500, err)
		return
	}
	s.writeJSON(w, 200, map[string]any{"saved": paths})
}

func (s *Server) upload(w http.ResponseWriter, r *http.Request) {
	to, err := strconv.ParseInt(r.URL.Query().Get("to"), 10, 64)
	if err != nil {
		s.writeErr(w, 400, fmt.Errorf("invalid to"))
		return
	}
	r.ParseMultipartForm(32 << 20)
	file, _, err := r.FormFile("file")
	if err != nil {
		s.writeErr(w, 400, fmt.Errorf("file part required"))
		return
	}
	tmp, err := os.CreateTemp("", "up-*")
	if err != nil {
		s.writeErr(w, 500, err)
		return
	}
	tmpName := tmp.Name()
	if _, err := io.Copy(tmp, file); err != nil {
		tmp.Close()
		s.writeErr(w, 500, err)
		return
	}
	tmp.Close()
	defer os.Remove(tmpName)

	caption := r.FormValue("caption")
	msg, err := s.svc.UploadFile(r.Context(), to, tmpName, caption)
	if err != nil {
		s.writeErr(w, 500, err)
		return
	}
	s.writeJSON(w, 200, map[string]any{"result": msg, "tmp": filepath.Base(tmpName)})
}

func (s *Server) forward(w http.ResponseWriter, r *http.Request) {
	var body struct {
		From int64   `json:"from"`
		To   int64   `json:"to"`
		IDs  []int64 `json:"ids"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		s.writeErr(w, 400, err)
		return
	}
	msg, err := s.svc.ForwardMessages(r.Context(), body.From, body.To, body.IDs)
	if err != nil {
		s.writeErr(w, 500, err)
		return
	}
	s.writeJSON(w, 200, map[string]any{"result": msg})
}

func (s *Server) transfers(w http.ResponseWriter, r *http.Request) {
	s.writeJSON(w, 200, map[string]any{"transfers": s.svc.Transfers()})
}

func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/status", s.status)
	mux.HandleFunc("POST /api/config", s.setConfig)
	mux.HandleFunc("POST /api/login/qr", s.startQRLogin)
	mux.HandleFunc("GET /api/login/qr.png", s.qrPNG)
	mux.HandleFunc("GET /api/login/qr/wait", s.waitQR)
	mux.HandleFunc("GET /api/login/status", s.qrStatus)
	mux.HandleFunc("POST /api/login/code", s.loginCode)
	mux.HandleFunc("POST /api/login/code/submit", s.loginCodeSubmit)
	mux.HandleFunc("POST /api/login/password", s.loginPassword)
	mux.HandleFunc("POST /api/logout", s.logout)
	mux.HandleFunc("GET /api/dialogs", s.dialogs)
	mux.HandleFunc("GET /api/messages", s.messages)
	mux.HandleFunc("POST /api/download", s.download)
	mux.HandleFunc("POST /api/upload", s.upload)
	mux.HandleFunc("POST /api/forward", s.forward)
	mux.HandleFunc("GET /api/transfers", s.transfers)

	// Static frontend
	static := http.FileServer(s.staticFS)
	mux.Handle("/", static)

	return logRequests(mux)
}

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		next.ServeHTTP(w, r)
	})
}
