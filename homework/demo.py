"""Sample homework for the demo school, so the module can be shown working."""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from . import services
from .models import DailyLimit, Task


def seed_homework(*, school, year, section, subject, teacher_user):
    """
    Give the demo school the homework module and a few tasks in one section: work due soon,
    work overdue, work checked and returned with feedback, and work handed in online. Does
    nothing if the school already has homework.
    """
    school.homework_enabled = True
    school.save(update_fields=["homework_enabled", "updated_at"])
    if Task.objects.filter(school=school).exists():
        return 0
    DailyLimit.objects.get_or_create(school=school, class_level=section.class_level, defaults={"minutes": 30})
    now = timezone.now()

    def make(title, *, due, set_at, minutes, purpose="", instructions="", **extra):
        task = Task(
            school=school,
            academic_year=year,
            class_level=section.class_level,
            subject=subject,
            title=title,
            purpose=purpose,
            instructions=instructions,
            estimated_minutes=minutes,
            **extra,
        )
        return services.save_task(user=teacher_user, task=task, targets={section: due}, action="publish", now=set_at)

    make(
        "Exercise 4.2, questions 1 to 10",
        due=now + timedelta(days=1),
        set_at=now - timedelta(hours=3),
        minutes=20,
        purpose="Practise adding fractions with different denominators.",
        instructions="Do questions 1 to 10 in your exercise book. Show every step.",
    )
    make(
        "Photograph your long division",
        due=now + timedelta(days=3),
        set_at=now - timedelta(hours=3),
        minutes=25,
        purpose="Show every step of long division.",
        instructions="Do questions 1 to 6, then take one clear photo of each page.",
        hand_in=Task.HandIn.ONLINE,
        marking=Task.Marking.MARKS,
        max_marks=Decimal(10),
    )
    checked = make(
        "Times tables 6 to 9",
        due=now - timedelta(days=2),
        set_at=now - timedelta(days=5),
        minutes=15,
        purpose="Recall the times tables quickly.",
        marking=Task.Marking.MARKS,
        max_marks=Decimal(10),
    )
    rows = []
    for number, row in enumerate(checked.submissions.select_related("enrollment")):
        done = number % 4 != 3
        rows.append(
            {
                "id": row.pk,
                "version": row.version,
                "status": "done" if done else "not_done",
                "late": False,
                "mark": Decimal(6 + number % 5) if done else None,
                "grade": "",
                "feedback": "Good recall; check 7 x 8." if done else "Please bring it tomorrow.",
                "reason": "",
            }
        )
    services.check_rows(
        user=teacher_user, task=checked, target=checked.targets.get(), rows=rows, return_work=True, now=now
    )
    return Task.objects.filter(school=school).count()
