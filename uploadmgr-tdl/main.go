package main

import (
	"context"
	"embed"
	"flag"
	"io/fs"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"tdlweb/internal/api"
	"tdlweb/internal/tg"
)

//go:embed static
var embeddedStatic embed.FS

func main() {
	addr := flag.String("http", ":8080", "listen address")
	dataDir := flag.String("data", "", "data directory")
	flag.Parse()

	if *dataDir == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			home = "/data"
		}
		*dataDir = filepath.Join(home, ".tdl-web")
	}

	svc, err := tg.NewService(*dataDir)
	if err != nil {
		log.Fatalf("init: %v", err)
	}
	defer svc.Close()

	sub, err := fs.Sub(embeddedStatic, "static")
	if err != nil {
		log.Fatalf("static: %v", err)
	}
	staticFS := http.FS(sub)

	srv := &http.Server{
		Addr:    *addr,
		Handler: api.New(svc, staticFS).Routes(),
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		<-ctx.Done()
		_ = srv.Shutdown(context.Background())
	}()

	log.Printf("tdl-web listening on %s (data: %s)", *addr, *dataDir)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server: %v", err)
	}
}
