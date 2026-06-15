#!/usr/bin/env python3
"""
diagnose.py — диагностика всех компонентов sync_music пайплайна.
Запуск: nix-shell shell.nix --run "python3 diagnose.py"
"""

import requests
import hashlib
import random
import string
import json
import os
import sys
import time
import uuid

# ─── Скопируй настройки из sync_music.py ─────────────────────────────────────
from config import (
    SLSKD_URL,
    SLSKD_USERNAME,
    SLSKD_PASSWORD,
    LITELLM_URL,
    HISTORY_FILE,
)

SEP = "─" * 60

def ok(msg):  print(f"  ✅  {msg}")
def err(msg): print(f"  ❌  {msg}")
def warn(msg):print(f"  ⚠️   {msg}")
def info(msg):print(f"  ℹ️   {msg}")


# ─── 1. SLSKD ─────────────────────────────────────────────────────────────────
def check_slskd():
    print(f"\n{SEP}")
    print("1. SLSKD")
    print(SEP)

    # 1a. Доступность
    try:
        resp = requests.get(f"{SLSKD_URL}/api/v0/application", timeout=5)
        ok(f"Хост доступен: {SLSKD_URL}  (HTTP {resp.status_code})")
    except requests.ConnectionError:
        err(f"Не удалось подключиться к {SLSKD_URL}")
        err("Проверь: docker ps | grep slskd  и  docker-compose.yaml порт 5030")
        return None
    except Exception as e:
        err(f"Ошибка подключения: {e}")
        return None

    # 1b. Авторизация
    try:
        resp = requests.post(
            f"{SLSKD_URL}/api/v0/session",
            json={"username": SLSKD_USERNAME, "password": SLSKD_PASSWORD},
            timeout=10
        )
        if resp.ok:
            token = resp.json().get("token")
            ok(f"Авторизация успешна (token: {token[:20]}...)")
        else:
            err(f"Авторизация провалилась: HTTP {resp.status_code}")
            err(f"Ответ: {resp.text[:200]}")
            err(f"Проверь SLSKD_USERNAME={SLSKD_USERNAME!r} и SLSKD_PASSWORD")
            return None
    except Exception as e:
        err(f"Ошибка авторизации: {e}")
        return None

    headers = {"Authorization": f"Bearer {token}"}

    # 1c. Текущие загрузки
    try:
        resp = requests.get(
            f"{SLSKD_URL}/api/v0/transfers/downloads",
            headers=headers, timeout=15
        )
        if resp.ok:
            all_transfers = []
            for user_entry in resp.json():
                username = user_entry.get("username", "")
                for d in user_entry.get("directories", []):
                    for f in d.get("files", []):
                        all_transfers.append({
                            "username": username,
                            "filename": os.path.basename(f.get("filename", "")),
                            "state":    f.get("state", ""),
                            "size":     f.get("size", 0),
                        })

            state_counts = {}
            for t in all_transfers:
                state_counts[t["state"]] = state_counts.get(t["state"], 0) + 1

            info(f"Всего трансферов в slskd: {len(all_transfers)}")
            for state, count in sorted(state_counts.items()):
                marker = "⚠️" if state in ("Errored", "TimedOut", "Cancelled") else "ℹ️"
                print(f"    {marker}  {state}: {count}")

            # Показываем Errored и долго Queued
            problematic = [t for t in all_transfers if t["state"] in ("Errored", "TimedOut", "Cancelled")]
            queued      = [t for t in all_transfers if t["state"] == "Queued"]
            if problematic:
                warn(f"Проблемных трансферов: {len(problematic)}")
                for t in problematic[:10]:
                    print(f"      [{t['state']}] {t['username']} / {t['filename']}")
            if queued:
                info(f"В очереди: {len(queued)}")
                for t in queued[:10]:
                    print(f"      [Queued] {t['username']} / {t['filename']}")
                if len(queued) > 10:
                    print(f"      ... и ещё {len(queued) - 10}")
        else:
            warn(f"Не удалось получить список загрузок: HTTP {resp.status_code}")
    except Exception as e:
        warn(f"Ошибка получения загрузок: {e}")

    # 1d. Тестовый поиск (короткий таймаут)
    print()
    info("Тестовый P2P поиск (10 секунд, запрос: 'linkin park in the end')...")
    search_id = str(uuid.uuid4())
    try:
        resp = requests.post(
            f"{SLSKD_URL}/api/v0/searches",
            headers=headers,
            json={"id": search_id, "searchText": "linkin park in the end"},
            timeout=10
        )
        if resp.ok:
            ok("Поисковый запрос отправлен в slskd")
            time.sleep(10)
            resp2 = requests.get(
                f"{SLSKD_URL}/api/v0/searches/{search_id}/responses",
                headers=headers, timeout=10
            )
            if resp2.ok:
                responses = resp2.json()
                total_files = sum(len(r.get("files", [])) for r in responses)
                flac_files  = sum(
                    1 for r in responses
                    for f in r.get("files", [])
                    if f.get("filename", "").lower().endswith(".flac")
                )
                ok(f"P2P поиск работает: {len(responses)} пиров, {total_files} файлов, {flac_files} FLAC")
                if flac_files == 0 and total_files > 0:
                    warn("FLAC файлы не найдены за 10с. За 90с их будет больше.")
                elif total_files == 0:
                    warn("За 10с ни одного ответа. Попробуй через 90с — возможно сеть медленная.")
            else:
                err(f"Не удалось получить результаты поиска: HTTP {resp2.status_code}")
        else:
            err(f"Ошибка отправки поиска: HTTP {resp.status_code} — {resp.text[:200]}")
    except Exception as e:
        err(f"P2P поиск упал с исключением: {e}")

    return token


# ─── 2. LITELLM ───────────────────────────────────────────────────────────────
def check_litellm():
    print(f"\n{SEP}")
    print("2. LiteLLM")
    print(SEP)

    # 2a. Доступность
    try:
        resp = requests.get(f"{LITELLM_URL}/health", timeout=5)
        ok(f"LiteLLM доступен: {LITELLM_URL}  (HTTP {resp.status_code})")
    except requests.ConnectionError:
        err(f"Не удалось подключиться к {LITELLM_URL}")
        err("Проверь: docker ps | grep litellm")
        return
    except Exception as e:
        warn(f"Health endpoint вернул ошибку: {e}")

    # 2b. Список моделей
    try:
        resp = requests.get(f"{LITELLM_URL}/v1/models", timeout=10)
        if resp.ok:
            models = [m["id"] for m in resp.json().get("data", [])]
            ok(f"Зарегистрированные модели: {models}")

            # Проверяем, есть ли нужная модель
            target = "gemini/gemini-2.5-flash"
            if target in models:
                ok(f"Модель {target!r} найдена")
            else:
                err(f"Модель {target!r} НЕ найдена в списке!")
                warn("В sync_music.py используется 'gemini/gemini-2.5-flash'")
                warn(f"Доступные имена: {models}")
                warn("→ Либо поменяй model_name в config.yaml LiteLLM,")
                warn("  либо поменяй 'model' в _llm_chat() в sync_music.py")
        else:
            warn(f"Не удалось получить список моделей: {resp.status_code}")
    except Exception as e:
        warn(f"Ошибка получения моделей: {e}")

    # 2c. Тестовый запрос
    print()
    info("Тестовый LLM запрос (модель: gemini/gemini-2.5-flash)...")
    try:
        t0   = time.time()
        resp = requests.post(
            f"{LITELLM_URL}/v1/chat/completions",
            json={
                "model":      "gemini/gemini-2.5-flash",
                "max_tokens": 10,
                "messages":   [{"role": "user", "content": "Reply with the single word: WORKING"}]
            },
            timeout=30
        )
        elapsed = time.time() - t0

        if resp.ok:
            answer = resp.json()["choices"][0]["message"]["content"].strip()
            ok(f"LLM ответил за {elapsed:.1f}с: {answer!r}")
        else:
            err(f"LLM вернул ошибку: HTTP {resp.status_code}")
            body = resp.json()
            err(f"Сообщение: {body.get('error', {}).get('message', resp.text[:300])}")

            # Специфичная диагностика частых ошибок
            text = resp.text.lower()
            if "api_key" in text or "401" in str(resp.status_code) or "invalid" in text:
                print()
                warn("═══ ОШИБКА API КЛЮЧА ════════════════════════")
                warn("Твой config.yaml содержит:")
                warn('  api_key: "os.environ/AIzaSyYourGeminiApiKeyHere"')
                warn("")
                warn("Это НЕВЕРНЫЙ синтаксис! LiteLLM интерпретирует это как:")
                warn('  os.getenv("AIzaSyYourGeminiApiKeyHere") → None')
                warn("")
                warn("Исправление (вариант A — ключ напрямую):")
                warn('  api_key: "AIzaSyYourGeminiApiKeyHere"')
                warn("")
                warn("Исправление (вариант B — через env var, рекомендуется):")
                warn("  В docker-compose.yaml добавь:")
                warn("    environment:")
                warn("      - GEMINI_API_KEY=AIzaSyYourGeminiApiKeyHere")
                warn("  В config.yaml:")
                warn('    api_key: "os.environ/GEMINI_API_KEY"')
                warn("═════════════════════════════════════════════")
            elif "model" in text or "not found" in text:
                warn("Возможно неверное имя модели. Проверь model_name в config.yaml")
    except requests.Timeout:
        err("LLM не ответил за 30 секунд (таймаут)")
        warn("Проверь: работает ли контейнер litellm? docker logs litellm")
    except Exception as e:
        err(f"LLM запрос упал с исключением: {e}")


# ─── 3. ИСТОРИЯ И SLSKD RECONCILE ─────────────────────────────────────────────
def check_history_vs_slskd(slskd_token):
    print(f"\n{SEP}")
    print("3. ИСТОРИЯ vs SLSKD (20 застрявших треков)")
    print(SEP)

    # История
    if not os.path.exists(HISTORY_FILE):
        err(f"История не найдена: {HISTORY_FILE}")
        return
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, list):
        completed = set(raw)
        pending   = {}
        failed    = set()
        info(f"Формат истории: v1 (список из {len(completed)} треков)")
    else:
        completed = set(raw.get("completed", []))
        pending   = raw.get("pending", {})
        failed    = set(raw.get("failed", []))
        info(f"Формат истории: v2")

    info(f"  completed: {len(completed)}, pending: {len(pending)}, failed: {len(failed)}")

    if not slskd_token:
        warn("slskd недоступен — пропускаем сравнение с очередью")
        return

    # Текущие trансферы в slskd
    headers = {"Authorization": f"Bearer {slskd_token}"}
    try:
        resp = requests.get(
            f"{SLSKD_URL}/api/v0/transfers/downloads",
            headers=headers, timeout=15
        )
        if not resp.ok:
            warn("Не удалось получить трансферы")
            return
        all_transfers = []
        for ue in resp.json():
            for d in ue.get("directories", []):
                for f in d.get("files", []):
                    all_transfers.append({
                        "username": ue.get("username", ""),
                        "filename": f.get("filename", ""),
                        "state":    f.get("state", ""),
                        "size":     f.get("size", 0),
                    })
    except Exception as e:
        warn(f"Ошибка: {e}")
        return

    active_states = {"Queued", "Requested", "Initializing", "InProgress"}
    error_states  = {"Errored", "TimedOut", "Cancelled"}
    active = [t for t in all_transfers if t["state"] in active_states]
    errors = [t for t in all_transfers if t["state"] in error_states]

    if not active and not errors:
        info("Активных трансферов в slskd нет")
        return

    print()
    warn(f"В slskd есть {len(active)} активных / {len(errors)} ошибочных трансферов")

    # Определяем, какие из них помечены как completed в истории (это и есть "потерянные")
    lost_tracks = []
    for t in (active + errors):
        fname_stem = os.path.splitext(os.path.basename(t["filename"]))[0].lower()
        for track_id in completed:
            parts = track_id.split(" - ", 1)
            if len(parts) != 2:
                continue
            title_norm = "".join(c for c in parts[1].lower() if c.isalnum())
            if title_norm and len(title_norm) >= 4 and title_norm in "".join(
                c for c in fname_stem if c.isalnum()
            ):
                lost_tracks.append((track_id, t))
                break

    if lost_tracks:
        warn(f"Найдено {len(lost_tracks)} треков: помечены как 'completed' но всё ещё в slskd:")
        for track_id, t in lost_tracks:
            print(f"    [{t['state']}] {track_id}")
            print(f"            ↳ {os.path.basename(t['filename'])}")
    else:
        info("Все активные трансферы slskd НЕ помечены как completed — всё нормально")

    if active:
        print()
        info("Активные трансферы (slskd качает/ждёт):")
        for t in active:
            print(f"    [{t['state']}] {os.path.basename(t['filename'])}")


# ─── 4. ИТОГ И РЕКОМЕНДАЦИИ ───────────────────────────────────────────────────
def print_summary():
    print(f"\n{SEP}")
    print("4. ЧТО ДЕЛАТЬ ДАЛЬШЕ")
    print(SEP)
    print("""
  A. Если LiteLLM показал ошибку API ключа:
     → Исправь config.yaml (см. fixed_litellm_config.yaml)
     → docker restart litellm
     → Запусти diagnose.py снова

  B. Если slskd недоступен:
     → docker ps | grep slskd
     → docker logs slskd
     → Проверь порт: curl http://localhost:5030/api/v0/application

  C. Если P2P поиск вернул 0 файлов за 10с:
     → Это нормально для короткого времени. В sync_music.py используется 90с.
     → Убедись что slskd подключён к Soulseek сети:
        открой http://localhost:5030 в браузере → вкладка Server

  D. Для запуска sync_music.py с подробным логом:
     nix-shell shell.nix --run "python3 sync_music.py 2>&1 | tee /tmp/sync_debug.log"
     tail -f /tmp/sync_debug.log

  E. Для восстановления 20 застрявших треков:
     Запусти: python3 recover_stuck.py
""")


def main():
    print("=" * 60)
    print("  sync_music диагностика")
    print("=" * 60)

    slskd_token = check_slskd()
    check_litellm()
    check_history_vs_slskd(slskd_token)
    print_summary()


if __name__ == "__main__":
    main()
