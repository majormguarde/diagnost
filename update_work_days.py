
from app import create_app
from app.models import OrganizationSettings
from app.extensions import db

app = create_app()
with app.app_context():
    settings = OrganizationSettings.get_settings()
    # If it's the old default or empty, set to Mon-Fri
    if not settings.work_days or settings.work_days == "1,2,3,4,5":
        settings.work_days = "0,1,2,3,4"
        db.session.commit()
        print("Updated existing settings to Mon-Fri (0,1,2,3,4)")
    else:
        print(f"Current work days: {settings.work_days}")
