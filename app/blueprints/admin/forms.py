from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed, FileRequired
from wtforms import StringField, TextAreaField, BooleanField, IntegerField, SelectMultipleField, SelectField, SubmitField, MultipleFileField, PasswordField, FloatField, DateTimeLocalField
from wtforms.validators import DataRequired, Optional, EqualTo, Length, NumberRange, Email, ValidationError

class MasterForm(FlaskForm):
    name = StringField("Имя мастера", validators=[DataRequired()])
    position = StringField("Должность", validators=[Optional()])
    description = TextAreaField("Описание", validators=[Optional()])
    is_active = BooleanField("Активен (Мастер доступен)", default=True)
    payout_percent = IntegerField("Процент от выполненных работ", validators=[Optional()], default=100)
    competency_ids = SelectMultipleField("Участки", coerce=int)
    submit = SubmitField("Сохранить")

class ClientForm(FlaskForm):
    name = StringField("Имя клиента", validators=[DataRequired(), Length(min=2, max=120)])
    phone = StringField("Логин (телефон)", validators=[DataRequired(), Length(min=6, max=32)])
    client_whatsapp = StringField("WhatsApp (номер)", validators=[Optional(), Length(max=32)])
    client_telegram = StringField("Telegram (ник без @)", validators=[Optional(), Length(max=64)])
    client_email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    password = PasswordField("Новый пароль", validators=[Optional(), Length(min=6, max=128)])
    password_confirm = PasswordField(
        "Подтверждение пароля",
        validators=[Optional()],
    )
    is_active = BooleanField("Активен", default=True)
    submit = SubmitField("Сохранить")

    def validate_password_confirm(self, field):
        """EqualTo ломается при пустом пароле / автозаполнении (None vs '')."""
        pw = (self.password.data or "").strip()
        conf = (field.data or "").strip()
        if conf and not pw:
            raise ValidationError("Очистите поле подтверждения или введите новый пароль.")
        if pw and conf != pw:
            raise ValidationError("Пароли должны совпадать.")


class ClientCreateForm(FlaskForm):
    name = StringField("Имя клиента", validators=[DataRequired(), Length(min=2, max=120)])
    phone = StringField("Логин (телефон)", validators=[DataRequired(), Length(min=6, max=32)])
    client_whatsapp = StringField("WhatsApp (номер)", validators=[Optional(), Length(max=32)])
    client_telegram = StringField("Telegram (ник без @)", validators=[Optional(), Length(max=64)])
    client_email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    password = PasswordField("Пароль", validators=[DataRequired(), Length(min=6, max=128)])
    password_confirm = PasswordField(
        "Подтверждение пароля",
        validators=[DataRequired(), EqualTo("password", message="Пароли должны совпадать")],
    )
    is_active = BooleanField("Активен", default=True)
    submit = SubmitField("Создать клиента")

class CompetencyForm(FlaskForm):
    title = StringField("Название участка", validators=[DataRequired()])
    submit = SubmitField("Сохранить")

class AppointmentForm(FlaskForm):
    master_id = SelectField("Мастер", coerce=int, validators=[DataRequired()])
    start_at = DateTimeLocalField("Начало приема", format='%Y-%m-%dT%H:%M', validators=[DataRequired()])
    status = SelectField("Статус", choices=[
        ("new", "Новая"),
        ("negotiation", "Согласование"),
        ("confirmed", "Подтверждена"),
        ("in_progress", "В работе"),
        ("done", "Выполнена"),
        ("cancelled_by_admin", "Отменена (админ)"),
        ("cancelled_by_client", "Отменена (клиент)"),
    ], validators=[DataRequired()])
    
    # Данные автомобиля
    car_make = StringField("Марка автомобиля", validators=[Optional()])
    car_model = StringField("Модель", validators=[Optional()])
    car_year = IntegerField("Год выпуска", validators=[Optional()])
    car_number = StringField("Гос.номер", validators=[Optional()])
    win_number = StringField("WIN номер", validators=[Optional(), Length(max=32)])
    engine_type = SelectField(
        "Тип двигателя",
        choices=[("", "—"), ("petrol", "Бензин"), ("diesel", "Дизель")],
        validators=[Optional()],
    )
    has_turbo = SelectField(
        "Турбина",
        choices=[("", "—"), ("yes", "Да"), ("no", "Нет")],
        validators=[Optional()],
    )
    engine_volume_l = FloatField("Объем двигателя (л)", validators=[Optional(), NumberRange(min=0.1, max=12.0)])
    transmission_type = SelectField(
        "Тип КПП",
        choices=[
            ("", "—"),
            ("manual", "Механика"),
            ("auto", "Автомат"),
            ("robot", "Робот"),
            ("cvt", "Вариатор"),
            ("other", "Другое"),
        ],
        validators=[Optional()],
    )
    mileage_km = IntegerField("Пробег (км)", validators=[Optional(), NumberRange(min=0, max=3000000)])
    
    # Описание проблемы
    problem_description = TextAreaField("Описание неисправностей", validators=[Optional()])
    
    submit = SubmitField("Сохранить")

class WorkForm(FlaskForm):
    title = StringField("Название операции", validators=[DataRequired()])
    category_id = SelectField("Категория операции", coerce=int, validators=[DataRequired()])
    duration_min = IntegerField("Длительность (мин)", validators=[DataRequired()], default=60)
    base_price = IntegerField("Базовая цена", validators=[Optional()])
    is_active = BooleanField("Активна", default=True)
    submit = SubmitField("Сохранить")

class CategoryForm(FlaskForm):
    competency_id = SelectField("Участок", coerce=int, validators=[DataRequired()])
    title = StringField("Название категории операции", validators=[DataRequired()])
    submit = SubmitField("Сохранить")

class WorkOrderForm(FlaskForm):
    status = SelectField("Статус заказа", choices=[
        ("draft", "Черновик"),
        ("opened", "Открыт"),
        ("closed", "Закрыт"),
        ("cancelled", "Отменен"),
    ], validators=[DataRequired()])
    total_amount = IntegerField("Итоговая сумма (руб.)", validators=[Optional()])
    inspection_results = TextAreaField("Результаты осмотра", validators=[Optional()])
    submit = SubmitField("Сохранить")

class DocumentUploadForm(FlaskForm):
    files = MultipleFileField("Выберите файлы", validators=[DataRequired()])
    submit = SubmitField("Загрузить")

class ContactSettingsForm(FlaskForm):
    """Мессенджеры сервиса, контактный email и SMTP (страница «Связь»)."""
    phone = StringField("Телефон организации (публичный)", validators=[Optional(), Length(max=32)])
    org_whatsapp = StringField("WhatsApp организации (номер)", validators=[Optional(), Length(max=32)])
    org_telegram = StringField("Telegram организации (ник без @)", validators=[Optional(), Length(max=64)])
    email = StringField("Email для связи", validators=[Optional(), Email(), Length(max=120)])
    smtp_host = StringField("SMTP сервер", validators=[Optional(), Length(max=120)])
    smtp_port = IntegerField("SMTP порт", validators=[Optional(), NumberRange(min=1, max=65535)])
    smtp_user = StringField("SMTP логин", validators=[Optional(), Length(max=120)])
    smtp_password = PasswordField("SMTP пароль", validators=[Optional(), Length(max=255)])
    smtp_use_tls = BooleanField("Использовать TLS", default=True)
    smtp_from = StringField("Адрес отправителя (From)", validators=[Optional(), Email(), Length(max=120)])
    telegram_bot_username = StringField("Имя Telegram-бота (без @)", validators=[Optional(), Length(max=64)])
    telegram_bot_token = PasswordField("Токен бота (BotFather)", validators=[Optional(), Length(max=255)])
    site_public_url = StringField("Публичный URL сайта (https://…)", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Сохранить")


class OrganizationSettingsForm(FlaskForm):
    name = StringField("Название организации", validators=[DataRequired()])
    address = StringField("Адрес", validators=[Optional()])
    phone = StringField("Телефон", validators=[Optional()])
    email = StringField("Email", validators=[Optional()])
    sbp_phone = StringField("Телефон для СБП", validators=[Optional(), Length(max=32)])
    work_hours = StringField("Часы работы", validators=[Optional()])
    slot_minutes = IntegerField("Интервал слота (мин)", validators=[DataRequired(), NumberRange(min=15, max=240)], default=60)
    description = TextAreaField("Описание (для сайта)", validators=[Optional()])
    latitude = FloatField("Широта (Latitude)", validators=[Optional()])
    longitude = FloatField("Долгота (Longitude)", validators=[Optional()])
    work_days = SelectMultipleField("Рабочие дни", choices=[
        ('0', 'Понедельник'),
        ('1', 'Вторник'),
        ('2', 'Среда'),
        ('3', 'Четверг'),
        ('4', 'Пятница'),
        ('5', 'Суббота'),
        ('6', 'Воскресенье')
    ], validators=[Optional()])
    submit = SubmitField("Сохранить настройки")


class AiAssistantSettingsForm(FlaskForm):
    """Настройки ИИ-помощника (админка)."""
    ai_provider = SelectField(
        "Провайдер",
        choices=[
            ("", "—"),
            ("openrouter", "router.ai / OpenRouter (OpenAI-compatible)"),
            ("openai", "OpenAI (OpenAI-compatible)"),
            ("custom", "Custom OpenAI-compatible"),
        ],
        validators=[Optional()],
    )
    ai_base_url = StringField("Base URL", validators=[Optional(), Length(max=255)])
    ai_api_key = PasswordField("API key", validators=[Optional(), Length(max=255)])
    ai_model = StringField("Модель (id)", validators=[Optional(), Length(max=120)])
    ai_site_url = StringField("Site URL (Referer)", validators=[Optional(), Length(max=255)])
    ai_app_name = StringField("App name (X-Title)", validators=[Optional(), Length(max=120)])
    submit = SubmitField("Сохранить")

class BannerForm(FlaskForm):
    title = StringField("Заголовок", validators=[Optional()])
    subtitle = StringField("Подзаголовок", validators=[Optional()])
    image = FileField("Изображение баннера", validators=[
        FileAllowed(['jpg', 'jpeg', 'png', 'webp'], "Только изображения (jpg, png, webp)")
    ])
    link = StringField("Ссылка (необязательно)", validators=[Optional()])
    order = IntegerField("Порядок вывода", default=0)
    is_active = BooleanField("Активен", default=True)
    submit = SubmitField("Сохранить баннер")

class ReviewForm(FlaskForm):
    author_name = StringField("Имя автора", validators=[DataRequired()])
    author_car = StringField("Автомобиль автора", validators=[Optional()])
    text = TextAreaField("Текст отзыва", validators=[DataRequired()])
    rating = IntegerField("Рейтинг (1-5)", default=5)
    is_published = BooleanField("Опубликован", default=False)
    submit = SubmitField("Сохранить отзыв")

class WorkOrderItemForm(FlaskForm):
    title = StringField("Наименование работы", validators=[DataRequired()])
    duration = IntegerField("Длительность (мин)", default=0)
    actual_duration = IntegerField("Фактическое время (мин)", default=0)
    price = IntegerField("Стоимость (руб.)", validators=[DataRequired()])
    is_done = BooleanField("Выполнено")
    master_id = SelectField("Исполнитель", coerce=int, validators=[Optional()])
    comment = TextAreaField("Комментарий", validators=[Optional()])
    submit = SubmitField("Добавить работу")

class WorkOrderPartForm(FlaskForm):
    title = StringField("Наименование запчасти/материала", validators=[DataRequired()])
    quantity = FloatField("Количество", default=1.0, validators=[DataRequired()])
    unit = StringField("Ед. изм.", default="шт.")
    price = IntegerField("Цена за ед. (руб.)", validators=[DataRequired()])
    submit = SubmitField("Добавить запчасть")

class WorkOrderDetailForm(FlaskForm):
    title = StringField("Наименование детали", validators=[DataRequired()])
    quantity = FloatField("Количество", default=1.0, validators=[DataRequired()])
    unit = StringField("Ед. изм.", default="шт.")
    price = IntegerField("Цена за ед. (руб.)", validators=[DataRequired()])
    submit = SubmitField("Добавить деталь")

class WorkOrderMaterialForm(FlaskForm):
    title = StringField("Наименование материала", validators=[DataRequired()])
    quantity = FloatField("Количество", default=1.0, validators=[DataRequired()])
    unit = StringField("Ед. изм.", default="шт.")
    price = IntegerField("Цена за ед. (руб.)", validators=[DataRequired()])
    submit = SubmitField("Добавить материал")


class WorkOrderAdditionalWorkForm(FlaskForm):
    title = StringField("Наименование", validators=[DataRequired()])
    price = IntegerField("Сумма (руб.)", validators=[DataRequired()])
    comment = TextAreaField("Комментарий", validators=[Optional()])
    submit = SubmitField("Добавить")

class AppointmentItemForm(FlaskForm):
    work_id = SelectField("Операция", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Добавить операцию")

class AdminCredentialsForm(FlaskForm):
    login = StringField("Новый логин (телефон)", validators=[DataRequired(), Length(min=5, max=20)])
    password = PasswordField("Новый пароль", validators=[Optional(), Length(min=6)])
    confirm_password = PasswordField("Подтверждение пароля", validators=[EqualTo('password', message='Пароли должны совпадать')])
    submit = SubmitField("Обновить данные входа")
