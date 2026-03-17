from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed, FileRequired
from wtforms import StringField, TextAreaField, BooleanField, IntegerField, SelectMultipleField, SelectField, SubmitField, MultipleFileField, PasswordField, FloatField, DateTimeLocalField
from wtforms.validators import DataRequired, Optional, EqualTo, Length

class MasterForm(FlaskForm):
    name = StringField("Имя мастера", validators=[DataRequired()])
    position = StringField("Должность", validators=[Optional()])
    description = TextAreaField("Описание", validators=[Optional()])
    is_active = BooleanField("Активен (Мастер доступен)", default=True)
    payout_percent = IntegerField("Процент от выполненных работ", validators=[Optional()], default=100)
    competency_ids = SelectMultipleField("Специализация", coerce=int)
    submit = SubmitField("Сохранить")

class CompetencyForm(FlaskForm):
    title = StringField("Название специализации", validators=[DataRequired()])
    submit = SubmitField("Сохранить")

class AppointmentForm(FlaskForm):
    master_id = SelectField("Мастер", coerce=int, validators=[DataRequired()])
    start_at = DateTimeLocalField("Начало приема", format='%Y-%m-%dT%H:%M', validators=[DataRequired()])
    status = SelectField("Статус", choices=[
        ("new", "Новая"),
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
    
    # Описание проблемы
    problem_description = TextAreaField("Описание неисправностей", validators=[Optional()])
    
    submit = SubmitField("Сохранить")

class WorkForm(FlaskForm):
    title = StringField("Название специализации", validators=[DataRequired()])
    category_id = SelectField("Категория", coerce=int, validators=[DataRequired()])
    duration_min = IntegerField("Длительность (мин)", validators=[DataRequired()], default=60)
    base_price = IntegerField("Базовая цена", validators=[Optional()])
    is_active = BooleanField("Активна", default=True)
    submit = SubmitField("Сохранить")

class CategoryForm(FlaskForm):
    title = StringField("Название категории", validators=[DataRequired()])
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

class OrganizationSettingsForm(FlaskForm):
    name = StringField("Название организации", validators=[DataRequired()])
    address = StringField("Адрес", validators=[Optional()])
    phone = StringField("Телефон", validators=[Optional()])
    email = StringField("Email", validators=[Optional()])
    work_hours = StringField("Часы работы", validators=[Optional()])
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

class AppointmentItemForm(FlaskForm):
    work_id = SelectField("Специализация", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Добавить специализацию")

class AdminCredentialsForm(FlaskForm):
    login = StringField("Новый логин (телефон)", validators=[DataRequired(), Length(min=5, max=20)])
    password = PasswordField("Новый пароль", validators=[Optional(), Length(min=6)])
    confirm_password = PasswordField("Подтверждение пароля", validators=[EqualTo('password', message='Пароли должны совпадать')])
    submit = SubmitField("Обновить данные входа")
