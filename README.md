# School ERP

Django 5.2 and Tailwind CSS school management application. The implemented workflows and known gaps are documented in [the DigiCampus comparison](docs/digicampus-comparison.md). This repository is a single school deployment today; records include a school foreign key so a future hosted version can isolate schools.

## Local setup (Windows PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py setup_roles
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py seed_demo --admin-username YOUR_SUPERUSER
.\tools\tailwindcss.exe -i .\static\src\input.css -o .\static\css\app.css --minify
.\.venv\Scripts\python.exe manage.py runserver
```

Log in at `/login/`. The demo command is repeatable, attaches the named existing superuser to the demo school, and does not send SMS or email. To use a real school instead, create it in Django admin, attach the account there, then configure academic years and classes under **Basic Settings**.

## Quality checks

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q --cov=core --cov=users --cov=academics --cov=students --cov=employees --cov=attendance --cov=fees --cov=finance --cov=examinations --cov=timetable --cov=downloads --cov=messaging --cov=holidays --cov=reports --cov-report=term:skip-covered
```

Browser tests use Playwright and a locally installed Chromium. Install with `.\.venv\Scripts\python.exe -m playwright install chromium` if needed. Other tests need no browser.

## Scheduled SMS

Sending queues messages in the database. The worker command processes up to 100 queued messages and can be scheduled by the operating system:

```powershell
.\.venv\Scripts\python.exe manage.py process_sms --limit 100
```

The default backend logs messages to the console. To use an HTTP gateway, set `SMS_BACKEND=messaging.backends.HttpSMSBackend` and configure the school's SMS URL, sender ID and key in Basic Settings. Delivery status currently means the gateway request succeeded; provider delivery receipts are not integrated.

## Deployment notes

Set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, and a PostgreSQL `DATABASE_URL`. Install a PostgreSQL driver separately if using PostgreSQL. Serve `staticfiles/` after `collectstatic` and keep `media/` private; uploaded files are served through permission checked views. Configure an email backend for password resets. Run `manage.py check --deploy` before deployment. Online payments and biometric devices need provider contracts and credentials and are tracked as open work in the comparison report.
