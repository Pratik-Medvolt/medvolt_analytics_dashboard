from django.contrib import admin

from .models import (
    Content,
    MailerLiteAnalytics,
    SearchConsoleAnalytics,
    SystemLog,
    WebsiteAnalytics,
    WeeklyReport,
)


admin.site.register(Content)
admin.site.register(WebsiteAnalytics)
admin.site.register(SearchConsoleAnalytics)
admin.site.register(MailerLiteAnalytics)
admin.site.register(WeeklyReport)
admin.site.register(SystemLog)