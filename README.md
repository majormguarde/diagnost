# Diagnost

Веб-приложение автосервиса для онлайн-записи на диагностику и автоэлектрику, управления расписанием мастеров, заявками, заказ-нарядами и клиентским кабинетом.

## Возможности продукта

- Публичная витрина: услуги, мастера, контакты, форма онлайн-записи
- Двухшаговая запись клиента: выбор мастера/даты и выбор доступного слота
- Личный кабинет клиента: заявки, детали визита, документы, интеграция с Telegram
- Админ-панель: управление мастерами, категориями и работами, отзывами, баннерами, заявками и заказ-нарядами
- SEO-оптимизация: мета-теги, canonical, OpenGraph/Twitter, `robots.txt`, `sitemap.xml`, JSON-LD (`AutoRepair`, `Service`, `FAQPage`)

## Технологии

- Python 3.11+
- Flask 3
- Flask-SQLAlchemy / SQLAlchemy 2
- Flask-Login
- Flask-WTF
- SQLite по умолчанию

## Установка и запуск

### 1) Клонирование

```bash
git clone https://github.com/majormguarde-bit/diagnost.git
cd diagnost
```

### 2) Виртуальное окружение

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3) Установка зависимостей

```bash
pip install -r requirements.txt
```

### 4) Настройка переменных окружения

Приложение работает и без `.env`, но для продакшн-конфигурации рекомендуется создать файл `.env` в корне:

```env
SECRET_KEY=change-me
DATABASE_URL=sqlite:///diagnost.sqlite3
DOCUMENTS_DIR=./var/documents
MAX_CONTENT_LENGTH=26214400
MIN_LEAD_TIME_MIN=30
CANCEL_BEFORE_HOURS=4
DEFAULT_DURATION_MIN=60
TELEGRAM_TOKEN_TTL_MIN=10
```

### 5) Инициализация базы

```bash
flask --app run.py init-db
```

### 6) Создание администратора

```bash
flask --app run.py create-admin --phone "+79990000000" --name "Администратор" --password "StrongPass123!"
```

### 7) Опционально: демо-данные

```bash
flask --app run.py seed-demo
```

### 8) Запуск

```bash
python run.py
```

После запуска приложение доступно по адресу: `http://127.0.0.1:5000/`

## Основные маршруты

- `/` — главная страница
- `/services` — услуги
- `/masters` — мастера
- `/contacts` — контакты
- `/booking/*` — шаги записи
- `/cabinet/*` — личный кабинет клиента
- `/admin/*` — административная часть

## SEO-эндпоинты

- `/robots.txt`
- `/sitemap.xml`

