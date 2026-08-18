package tg

import (
	"context"

	"go.etcd.io/bbolt"
)

// boltSession implements telegram.session.Store storage on top of a single
// bbolt database. This keeps the MTProto session across restarts so the user
// stays logged in.
type boltSession struct {
	db *bbolt.DB
}

var sessionBucket = []byte("session")

func (s *boltSession) LoadSession(_ context.Context) ([]byte, error) {
	var out []byte
	err := s.db.View(func(tx *bbolt.Tx) error {
		b := tx.Bucket(sessionBucket)
		if b == nil {
			return nil
		}
		out = append(out, b.Get([]byte("data"))...)
		return nil
	})
	return out, err
}

func (s *boltSession) StoreSession(_ context.Context, data []byte) error {
	return s.db.Update(func(tx *bbolt.Tx) error {
		b, err := tx.CreateBucketIfNotExists(sessionBucket)
		if err != nil {
			return err
		}
		return b.Put([]byte("data"), data)
	})
}

func (s *boltSession) clear() error {
	return s.db.Update(func(tx *bbolt.Tx) error {
		return tx.DeleteBucket(sessionBucket)
	})
}
