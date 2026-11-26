# 🎧 Roblox Audio ID Finder — Telegram Bot + GitHub Actions Scanner

Телеграм-бот для поиска рабочих **Roblox Audio ID** песен с монетизацией через **Telegram Stars (`⭐ XTR`)**, автоматическим сканером в **GitHub Actions** и бессерверным хостингом на **Vercel + Turso (0 ₽ на старте)**.

---

## ✨ Возможности

* **Автосканер в GitHub Actions (`scanner/roblox_scanner.py`):**
  * За один ран проверяет до **`10 000` числовых ID** аудио в Roblox с плавной задержкой между запросами (`0.8 сек`) + индексирует публичный каталог Creator Store по популярным исполнителям и жанрам, а также отключает удалённые треки (`[ Content Deleted ]`).
  * **Умное распознавание песен (`scanner/identifier.py`):** если доступно аудио-превью — распознаёт трек по звуку через **Shazam (`shazamio`)**, а зашифрованные/транслитные метаданные (`cpsze 17 nozhevih`) приводит к чистому виду (`CUPSIZE - семнадцать ножевых`) через открытые музыкальные каталоги **iTunes + Deezer API** и нечёткое сравнение.
* **Сверхлёгкий поиск похожих треков на Vercel (`core/matcher.py`):**
  * Весит `0 МБ` лишних зависимостей, отвечает за миллисекунды, понимает опечатки, транслит в обе стороны (`капсайз` ↔ `CUPSIZE`, `шадоурейз` ↔ `Shadowraze`) и числа прописью (`17` ↔ `семнадцать`).
* **Монетизация и лимиты:**
  * **5 успешных поисков в сутки** бесплатно (сброс в `00:00 МСК`).
  * Если по запросу **ничего не найдено** — попытка **НЕ списывается**.
  * **Полный иммунитет к лимитам** и встроенная инлайн **Админ-панель** для Telegram ID `5188800998`.
  * Покупка пакетов поисков и VIP-безлимита в 1 клик за **Telegram Stars (`XTR`)** прямо внутри бота.
  * Формат выдачи с нативной кнопкой **`📋 Скопировать ID`** (`copy_text` в Telegram Bot API) и листанием похожих вариантов без повторного списания лимита.

---

## 🚀 Быстрый запуск за 5 минут (0 ₽)

### 1. Создайте бесплатную базу данных Turso (SQLite over HTTP)
1. Зайдите на [turso.tech](https://turso.tech) (вход через GitHub).
2. Создайте базу данных (например, `roblox-audio`) и нажмите **Generate Token**.
3. Скопируйте `TURSO_DATABASE_URL` (`libsql://...`) и `TURSO_AUTH_TOKEN`.

### 2. Создайте бота в `@BotFather`
1. Отправьте `/newbot` в `@BotFather` и скопируйте `TELEGRAM_BOT_TOKEN`.
2. *(Никакие платёжные провайдеры подключать не нужно — **Telegram Stars (`XTR`)** работают сразу из коробки!)*

### 3. Добавьте секреты в GitHub Actions (для автосканера)
В репозитории откройте **Settings → Secrets and variables → Actions → New repository secret**:
* `TURSO_DATABASE_URL` — URL базы Turso
* `TURSO_AUTH_TOKEN` — токен базы Turso
* `ROBLOX_API_KEY` *(опционально, но рекомендуется)* — бесплатный ключ с [create.roblox.com/dashboard/credentials](https://create.roblox.com/dashboard/credentials) с правом чтения `Assets`

После этого во вкладке **Actions → Roblox Audio ID Scanner → Run workflow** можно запустить первый сбор базы вручную (а дальше он будет крутиться сам по расписанию).

### 4. Задеплойте бота на Vercel
1. Зайдите на [vercel.com/new](https://vercel.com/new) и импортируйте этот GitHub-репозиторий.
2. В блоке **Environment Variables** укажите:
   * `TELEGRAM_BOT_TOKEN` — токен от `@BotFather`
   * `WEBHOOK_SECRET` — любое секретное слово латиницей (например, `my_super_secret_777`)
   * `TURSO_DATABASE_URL` — URL базы Turso
   * `TURSO_AUTH_TOKEN` — токен базы Turso
   * `ADMIN_IDS` — `5188800998` *(уже вшит по умолчанию, через запятую можно добавить ещё админов)*
3. Нажмите **Deploy**.
4. После деплоя **один раз** откройте в браузере ссылку:
   ```text
   https://<ваш-домен>.vercel.app/api/setup?secret=<ваш_WEBHOOK_SECRET>
   ```
   Эта ссылка автоматически создаст таблицы в Turso и привяжет вебхук в Telegram. Готово — бот работает!

---

## 📁 Структура проекта

```text
├── api/
│   └── index.py               # FastAPI вебхук для Vercel (/api/webhook, /api/setup)
├── core/
│   ├── bot_handlers.py        # Инлайн-интерфейс бота, квоты, Stars XTR и админка
│   ├── config.py              # Конфигурация, тарифы Звёзд, иммунитет ID 5188800998
│   ├── db.py                  # Запросы к Turso, лимиты 5/сутки, нечёткий поиск
│   ├── matcher.py             # Движок сходства названий (триграммы + фонетика + транслит)
│   ├── music_resolver.py      # Определение эталонного артиста/трека через iTunes и Deezer
│   ├── roblox_api.py          # Проверка существования и типа аудио в Roblox
│   ├── telegram.py            # Клиент Telegram Bot API (copy_text, Stars XTR)
│   ├── text_utils.py          # Транслитерация RU↔EN, числительные (17↔семнадцать), очистка
│   └── turso.py               # Легковесный асинхронный HTTP-клиент Turso
├── scanner/
│   ├── identifier.py          # Распознавание аудио через Shazam + музыкальные каталоги
│   └── roblox_scanner.py      # Сканер 150 000 ID за ран + поиск по каталогу Creator Store
├── tests/
│   └── test_core.py           # Автотесты логики лимитов, админки и нечёткого поиска
└── .github/workflows/
    ├── ci.yml                 # Автопроверка кода и тестов при каждом пуше
    └── scanner.yml            # Воркфлоу сканера (cron + ручной запуск, таймаут до 10ч)
```
