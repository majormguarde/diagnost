from datetime import datetime

from flask import Blueprint, jsonify, request, current_app
from werkzeug.security import check_password_hash

from ...extensions import csrf, db
from ...models import TelegramLinkToken, TelegramLink, WorkOrder, User

bp = Blueprint("telegram", __name__)


@bp.get("/status")
def status():
    return jsonify({"ok": True, "bot_name": current_app.config.get("TELEGRAM_BOT_NAME", "AutoDiagBot")})


@bp.post("/webhook")
@csrf.exempt
def webhook():
    # В реальном приложении здесь будет обработка обновлений от Telegram
    # Для демонстрации реализуем логику связки аккаунта
    
    data = request.get_json(silent=True) or {}
    message = data.get("message", {})
    text = (message.get("text") or "").strip()
    chat_id = message.get("chat", {}).get("id")
    
    if not chat_id:
        return jsonify({"ok": True})
        
    if text.startswith("/start"):
        parts = text.split()
        if len(parts) > 1:
            # Попытка привязки по коду: /start <TOKEN>
            token_str = parts[1]
            return _link_account(chat_id, token_str)
        else:
            return _send_reply(chat_id, "Привет! Для привязки аккаунта введите код из личного кабинета или используйте ссылку из кабинета.")
            
    # Другие команды: список заказов и т.д.
    if text == "Мои заказы":
        return _list_orders(chat_id)
        
    return jsonify({"ok": True})


def _link_account(chat_id, token_str):
    # Находим токен (хэшированный в БД)
    # В реальности нам пришлось бы перебирать или использовать другой способ,
    # но для простоты мы можем искать активные токены
    tokens = db.session.execute(
        db.select(TelegramLinkToken).where(TelegramLinkToken.used_at.is_(None))
    ).scalars().all()
    
    found_token = None
    for t in tokens:
        if check_password_hash(t.token_hash, token_str):
            if t.expires_at > datetime.utcnow():
                found_token = t
                break
                
    if not found_token:
        return _send_reply(chat_id, "Неверный или просроченный код.")
        
    # Создаем связь
    link = db.session.execute(
        db.select(TelegramLink).where(TelegramLink.user_id == found_token.user_id)
    ).scalar_one_or_none()
    
    if not link:
        link = TelegramLink(user_id=found_token.user_id, telegram_chat_id=str(chat_id))
        db.session.add(link)
    else:
        link.telegram_chat_id = str(chat_id)
        link.is_active = True
        link.linked_at = datetime.utcnow()
        
    found_token.used_at = datetime.utcnow()
    db.session.commit()
    
    user = db.session.get(User, found_token.user_id)
    return _send_reply(chat_id, f"Аккаунт успешно привязан: {user.name} ({user.phone})")


def _list_orders(chat_id):
    link = db.session.execute(
        db.select(TelegramLink).where(TelegramLink.telegram_chat_id == str(chat_id), TelegramLink.is_active.is_(True))
    ).scalar_one_or_none()
    
    if not link:
        return _send_reply(chat_id, "Аккаунт не привязан. Сначала привяжите аккаунт в личном кабинете.")
        
    orders = db.session.execute(
        db.select(WorkOrder).where(WorkOrder.client_user_id == link.user_id).order_by(WorkOrder.id.desc()).limit(5)
    ).scalars().all()
    
    if not orders:
        return _send_reply(chat_id, "У вас пока нет заказ-нарядов.")
        
    reply = "Ваши последние заказы:\n"
    for o in orders:
        status_ru = {"draft": "Черновик", "opened": "Открыт", "closed": "Закрыт", "cancelled": "Отменен"}.get(o.status, o.status)
        reply += f"№{o.id} от {o.created_at.strftime('%d.%m.%Y')} — {status_ru}. Сумма: {o.total_amount or 0} руб.\n"
        if o.documents:
            reply += "  Документы доступны для скачивания.\n"
            
    return _send_reply(chat_id, reply)


def _send_reply(chat_id, text):
    # В реальном боте здесь был бы вызов Telegram API (requests.post)
    # Для демонстрации возвращаем JSON, который "бот" мог бы использовать
    return jsonify({
        "ok": True,
        "action": "send_message",
        "chat_id": chat_id,
        "text": text
    })
