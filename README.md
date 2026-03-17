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

## Production deploy

Ниже пример базового продакшн-развертывания на Linux: `Nginx` как reverse proxy и `gunicorn` или `uWSGI` как WSGI-сервер.

### Вариант A: gunicorn + nginx

1) Установите gunicorn:

```bash
pip install gunicorn
```

2) Запуск приложения через gunicorn:

```bash
gunicorn -w 4 -b 127.0.0.1:8000 wsgi:app
```

3) Пример блока сайта в Nginx (`/etc/nginx/sites-available/diagnost`):

```nginx
server {
    listen 80;
    server_name your-domain.example;

    client_max_body_size 25m;

    location /static/ {
        alias /opt/diagnost/app/static/;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Вариант B: uWSGI + nginx

1) Установите uWSGI:

```bash
pip install uwsgi
```

2) Запуск uWSGI:

```bash
uwsgi --http 127.0.0.1:8000 --module wsgi:app --master --processes 4 --threads 2
```

3) Nginx-конфиг можно использовать тот же, что и для gunicorn.

### Рекомендации для production

- Используйте сильный `SECRET_KEY` в `.env`.
- Храните БД и документы на постоянном диске с резервным копированием.
- Включите HTTPS (Let’s Encrypt + certbot).
- Запускайте WSGI-сервер под systemd/supervisor для автоперезапуска.
- Не используйте `debug=True` в продакшене.

## Troubleshooting

### 1) `flask --app run.py init-db` не выполняется

- Убедитесь, что активировано виртуальное окружение.
- Проверьте установку зависимостей: `pip install -r requirements.txt`.
- Проверьте, что команда запускается из корня проекта.

### 2) Ошибка `ModuleNotFoundError` при запуске

- Обычно проблема в неактивном `.venv` или неполной установке пакетов.
- Повторно активируйте окружение и установите зависимости.

### 3) Ошибка доступа к БД SQLite

- Проверьте права на файл `diagnost.sqlite3` и директорию проекта.
- Если задаете `DATABASE_URL`, убедитесь в корректности строки подключения.

### 4) Не загружаются файлы / документы

- Проверьте `DOCUMENTS_DIR` и права на запись.
- Значение по умолчанию: `./var/documents`.

### 5) 403 CSRF при отправке форм

- Убедитесь, что формы рендерят `{{ form.hidden_tag() }}`.
- Проверьте, что приложение открывается по одному и тому же домену/схеме, особенно за reverse proxy.

### 6) В продакшене 502 Bad Gateway через Nginx

- Проверьте, что gunicorn/uWSGI запущен и слушает ожидаемый порт.
- Сверьте `proxy_pass` в Nginx с фактическим адресом WSGI-сервера.
- Проверьте логи Nginx и процесса приложения.
