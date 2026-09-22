from datetime import timedelta
from django.http import HttpResponse
from core.access import require_permission
from .models import Holiday
def ical_escape(text):
    return text.replace("\\","\\\\").replace(";","\\;").replace(",","\\,").replace("\r","").replace("\n","\\n")
@require_permission("holidays.view_holiday")
def calendar_export(request):
    lines = ["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//School ERP//School Calendar//EN"]
    for h in Holiday.objects.filter(school=request.school):
        lines += ["BEGIN:VEVENT",f"UID:holiday-{h.pk}@school-erp",f"DTSTAMP:{h.updated_at:%Y%m%dT%H%M%SZ}",
                  f"DTSTART;VALUE=DATE:{h.start_date:%Y%m%d}",f"DTEND;VALUE=DATE:{h.end_date+timedelta(days=1):%Y%m%d}",
                  "SUMMARY:"+ical_escape(h.name),"DESCRIPTION:"+ical_escape(h.description),"END:VEVENT"]
    lines.append("END:VCALENDAR")
    response=HttpResponse("\r\n".join(lines)+"\r\n",content_type="text/calendar")
    response["Content-Disposition"]='attachment; filename="school-calendar.ics"'
    return response
