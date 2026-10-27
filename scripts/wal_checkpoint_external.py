#!/usr/bin/env python3
"""Trunca o WAL do state.db e verifica integridade. RODAR FORA DO GATEWAY.

Contexto (validado 2026-08-27): o gateway do Hermes mantem um snapshot de leitura
permanente sobre o state.db, e o SQLite nao recicla paginas do WAL ainda visiveis a
um leitor. Resultado: `PRAGMA wal_checkpoint(...)` retorna busy=0 (SEM erro) mas
travado em ~13% das paginas, indefinidamente. Um WAL de 1,15 GB ficou inerte por
~46 tentativas de dentro do app.

`hermes gateway restart/stop` e BLOQUEADO de dentro do proprio gateway (guarda
arquitetural). Uma sessao de chat do Hermes Desktop roda DENTRO do gateway, entao a
Eva nao consegue resolver isso sozinha. Uso correto, num cmd/PowerShell externo:

    hermes gateway stop
    python %USERPROFILE%\\.hermes\\scripts\\wal_checkpoint_external.py
    hermes gateway start

Resultado medido no incidente: WAL 1.154 MB -> 0,0 MB; total em disco 1.812 -> 657 MB.

NOTA: `journal_size_limit` e PRAGMA POR CONEXAO e NAO persiste no arquivo. Este
script o aplica, mas processos novos voltam ao default (-1 = ilimitado). Com gateway
24/7 o WAL reacumula: isso e manutencao recorrente, nao fix definitivo.
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

H = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB = H / "state.db"
WAL = H / "state.db-wal"
SHM = H / "state.db-shm"

# 64 MB: teto pro WAL devolver espaco ao SO depois do checkpoint.
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
MAX_ATTEMPTS = 40


def mb(path: Path) -> float:
    return path.stat().st_size / 1024 / 1024 if path.exists() else 0.0


def total_on_disk() -> float:
    return sum(mb(p) for p in (DB, WAL, SHM))


def main() -> int:
    if not DB.exists():
        print(f"ERRO: {DB} nao encontrado.")
        return 1

    print(f"db={mb(DB):.1f} MB  wal={mb(WAL):.1f} MB  shm={mb(SHM):.1f} MB")
    print(f"TOTAL em disco: {total_on_disk():.1f} MB")
    print("(ls -la no state.db MENTE: sempre somar os tres arquivos)\n")

    con = sqlite3.connect(str(DB), timeout=60, isolation_level=None)
    cur = con.cursor()
    cur.execute("PRAGMA busy_timeout = 60000")

    cur.execute("PRAGMA journal_size_limit")
    print(f"journal_size_limit atual: {cur.fetchone()[0]}  (-1 = ilimitado = causa-raiz)")
    cur.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT}")
    cur.execute("PRAGMA wal_autocheckpoint = 1000")

    truncated = False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        for mode in ("TRUNCATE", "RESTART", "FULL"):
            try:
                cur.execute(f"PRAGMA wal_checkpoint({mode})")
                busy, log, ckpt = cur.fetchone()
            except sqlite3.OperationalError as exc:
                print(f"  [{attempt:2d}] {mode:8s} erro: {exc}", flush=True)
                continue

            size = mb(WAL)
            print(
                f"  [{attempt:2d}] {mode:8s} busy={busy} log={log} ckpt={ckpt} WAL={size:.1f} MB",
                flush=True,
            )
            # busy=0 NAO garante sucesso: comparar log==ckpt E o tamanho do arquivo.
            if busy == 0 and log == 0 and ckpt == 0 and size < 5:
                print(f"\nOK: WAL truncado para {size:.1f} MB")
                truncated = True
                break
        if truncated:
            break
        time.sleep(3)

    if not truncated:
        print(f"\nWAL final: {mb(WAL):.1f} MB — nao truncou.")
        print("Causa mais provavel: o gateway AINDA esta rodando.")
        print("Rode `hermes gateway stop` num shell externo e tente de novo.")
        con.close()
        return 2

    con.close()

    # Verificacao de integridade OBRIGATORIA antes de declarar sucesso.
    print("\n=== INTEGRIDADE ===")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    cur = con.cursor()
    cur.execute("PRAGMA quick_check")
    check = cur.fetchone()[0]
    print(f"quick_check: {check}")
    cur.execute("PRAGMA foreign_key_check")
    print(f"foreign_key_check: {cur.fetchall() or 'OK'}")

    cur.execute("SELECT COUNT(*) FROM sessions")
    sessions = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM messages")
    messages = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM messages_fts")
    fts = cur.fetchone()[0]
    print(f"sessions={sessions:,}  messages={messages:,}  messages_fts={fts:,}")
    if messages != fts:
        print("  AVISO: messages != messages_fts (indice dessincronizado).")
        print("  Rodar: hermes sessions optimize")

    # Se freelist for baixo, VACUUM e perda de tempo — medir antes de sugerir.
    cur.execute(
        "SELECT freelist_count * page_size / 1024.0 / 1024 "
        "FROM pragma_freelist_count(), pragma_page_size()"
    )
    print(f"freelist (recuperavel por VACUUM): {cur.fetchone()[0]:.1f} MB")
    con.close()

    print(f"\nTOTAL em disco agora: {total_on_disk():.1f} MB")
    print("Lembrete: journal_size_limit nao persiste — repetir quando o WAL crescer.")
    return 0 if check == "ok" else 3


if __name__ == "__main__":
    sys.exit(main())
