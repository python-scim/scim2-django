SECRET_KEY = "change-me"
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
ROOT_URLCONF = "examples.urls"
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]
INSTALLED_APPS = ["scim2_django", "examples"]
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "db.sqlite3"},
}
