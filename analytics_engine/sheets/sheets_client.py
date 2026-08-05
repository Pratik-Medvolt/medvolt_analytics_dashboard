import gspread
from decouple import config
from google.oauth2.service_account import Credentials
from datetime import datetime


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_spreadsheet():
    creds = Credentials.from_service_account_file(
        config("GOOGLE_CREDENTIALS_FILE"),
        scopes=SCOPES,
    )

    client = gspread.authorize(creds)
    return client.open(config("GOOGLE_SHEET_NAME"))


def get_content_library_sheet():
    return get_spreadsheet().worksheet("content_library")


def read_content_library():
    sheet = get_content_library_sheet()
    return sheet.get_all_records()


def append_content_rows(rows):
    if not rows:
        return

    sheet = get_content_library_sheet()
    sheet.append_rows(rows, value_input_option="USER_ENTERED")

def read_content_library():
    sheet = get_spreadsheet().worksheet("content_library")
    return sheet.get_all_records()

def append_ga4_rows(rows):
    if not rows:
        return

    sheet = get_spreadsheet().worksheet("ga4_blog_analytics")
    sheet.append_rows(rows, value_input_option="USER_ENTERED")

def append_search_console_rows(rows):
    if not rows:
        return

    sheet = get_spreadsheet().worksheet("search_console_analytics")
    sheet.append_rows(rows, value_input_option="USER_ENTERED")

def append_mailerlite_rows(rows):
    if not rows:
        return

    sheet = get_spreadsheet().worksheet("mailerlite_campaign_analytics")
    sheet.append_rows(rows, value_input_option="USER_ENTERED")

def clear_mailerlite_analytics_sheet():
    sheet = get_spreadsheet().worksheet("mailerlite_campaign_analytics")

    all_values = sheet.get_all_values()

    if len(all_values) > 1:
        sheet.delete_rows(2, len(all_values))



def append_log(module, status, records_processed=0, error_message=""):
    sheet = get_spreadsheet().worksheet("system_logs")

    sheet.append_row([
        datetime.now().isoformat(),
        module,
        status,
        records_processed,
        error_message,
    ])


def read_ga4_analytics():
    sheet = get_spreadsheet().worksheet("ga4_blog_analytics")
    return sheet.get_all_records()


def read_mailerlite_analytics():
    sheet = get_spreadsheet().worksheet("mailerlite_campaign_analytics")
    return sheet.get_all_records()


def clear_weekly_report_sheet():
    sheet = get_spreadsheet().worksheet("weekly_report")
    all_values = sheet.get_all_values()

    if len(all_values) > 1:
        sheet.delete_rows(2, len(all_values))


def append_weekly_report_rows(rows):
    if not rows:
        return

    sheet = get_spreadsheet().worksheet("weekly_report")
    sheet.append_rows(rows, value_input_option="USER_ENTERED")