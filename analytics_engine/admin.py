from django.contrib import admin

from .models import (
    Content,
    GA4DailyMetric,
    GA4Report,
    GA4ReportRow,
    MailerLiteAnalytics,
    SearchConsoleAnalytics,
    SystemLog,
    WebsiteAnalytics,
    WeeklyReport,
)


admin.site.register(Content)
admin.site.register(WebsiteAnalytics)
admin.site.register(GA4DailyMetric)
admin.site.register(GA4Report)
admin.site.register(GA4ReportRow)
admin.site.register(SearchConsoleAnalytics)
admin.site.register(MailerLiteAnalytics)
admin.site.register(WeeklyReport)
admin.site.register(SystemLog)