from django.apps import AppConfig
from django.db.backends.signals import connection_created


def enable_sqlite_wal(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")


class AnalyticsEngineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "analytics_engine"
    verbose_name = "Medvolt Analytics"

    def ready(self):
        connection_created.connect(
            enable_sqlite_wal,
            dispatch_uid="analytics_engine.enable_sqlite_wal",
        )
