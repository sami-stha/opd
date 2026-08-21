from pathlib import Path
from decouple import config
import os
BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY', default='django-insecure-dev-key-change-in-production')

DEBUG = config('DEBUG', default=True, cast=bool)

# SMS — SMS_DEBUG=True uses dummy OTP (no real SMS). Set False + Sparrow credentials for live delivery.
SMS_DEBUG = config('SMS_DEBUG', default=True, cast=bool)
SMS_DUMMY_OTP = config('SMS_DUMMY_OTP', default='123456')
SMS_PROVIDER = config('SMS_PROVIDER', default='sparrow')  # sparrow | twilio
SMS_API_KEY = config('SMS_API_KEY', default='')           # Sparrow API token
SMS_SENDER_ID = config('SMS_SENDER_ID', default='')       # Sparrow approved sender ID
SPARROW_SMS_URL = config('SPARROW_SMS_URL', default='http://api.sparrowsms.com/v2/sms/')
TWILIO_ACCOUNT_SID = config('TWILIO_ACCOUNT_SID', default='')
TWILIO_AUTH_TOKEN = config('TWILIO_AUTH_TOKEN', default='')
TWILIO_FROM_NUMBER = config('TWILIO_FROM_NUMBER', default='')

# eSewa ePay (UAT defaults — register merchant for production keys)
SITE_BASE_URL = config('SITE_BASE_URL', default='http://127.0.0.1:8000')
ESEWA_PRODUCT_CODE = config('ESEWA_PRODUCT_CODE', default='EPAYTEST')
ESEWA_SECRET_KEY = config('ESEWA_SECRET_KEY', default='8gBm/:&EnhH.1/q')
ESEWA_UAT = config('ESEWA_UAT', default=True, cast=bool)
ESEWA_SKIP_SIGNATURE_VERIFY = config('ESEWA_SKIP_SIGNATURE_VERIFY', default=False, cast=bool)

ALLOWED_HOSTS = ['127.0.0.1', 'localhost', "opd.onrender.com",]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'django_filters',

    'accounts',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Asia/Kathmandu'

USE_I18N = True

USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'accounts.authentication.BearerTokenAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
}

CSRF_COOKIE_HTTPONLY = False
SESSION_COOKIE_SAMESITE = 'Lax'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'accounts.User'

CORS_ALLOW_ALL_ORIGINS = True

STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'opd-new frontend'),
]

# Fast isolated DB for automated tests (avoids PostgreSQL test DB conflicts).
if os.environ.get('DJANGO_RUNNING_TESTS') == '1':
    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }

    class _DisableMigrations:
        def __contains__(self, item):
            return True

        def __getitem__(self, item):
            return None

    MIGRATION_MODULES = _DisableMigrations()

    ESEWA_SKIP_SIGNATURE_VERIFY = True

    

if os.environ.get("DATABASE_URL"):
    # Render
    DATABASES = {
        "default": dj_database_url.config(
            conn_max_age=600,
            ssl_require=True,
        )
    }
else:
    # Local computer
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": config("DB_NAME", default="postgres"),
            "USER": config("DB_USER", default="postgres"),
            "PASSWORD": config("DB_PASSWORD", default=""),
            "HOST": config("DB_HOST", default="localhost"),
            "PORT": config("DB_PORT", default="5432"),
        }
    }