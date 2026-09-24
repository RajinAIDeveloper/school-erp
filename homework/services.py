"""
Setting homework, keeping each task's list of students right, and recording what was done.

Every function takes `now`, so what counts as due, late or live is decided by the caller and
tests do not depend on the clock.
"""

from datetime import datetime, time, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext

from core.models import AuditLog

from .access import employee_of, may_manage, may_mark, may_set
from .models import DailyLimit, Submission, SubmissionFile, Task, TaskResource, TaskSection

SUGGEST_DAYS = 28  # how far ahead the next lesson is looked for
DEFAULT_START = time(9, 0)  # the due time when the school has no periods set up
# Fixed once any student has a record: changing them would change what the records mean.
LOCKED_FIELDS = ("subject_id", "class_level_id", "marking", "max_marks", "selected_only")


def _audit(task, user, action, description):
    AuditLog.objects.create(
        school=task.school,
        user=user,
        action=action,
        model=Task._meta.label,
        object_id=str(task.pk),
        description=description,
    )


def _at(day, moment):
    return timezone.make_aware(datetime.combine(day, moment))


# ------------------------------------------------------------------ who is expected


def _takes(enrollment, subject, papers, plan):
    """Whether this student takes the subject: any of its papers, for a subject taught in papers."""
    from examinations.subjects import paper_role

    if papers:
        return any(paper_role(enrollment, paper, plan) for paper in papers)
    return paper_role(enrollment, subject, plan) is not None


def eligible(academic_year, class_level, subject, sections):
    """
    The students of these sections who take the subject, in section and roll order.

    Groups, choice subjects, the 4th subject and religion papers all count: a Humanities
    student is never set Physics, and a Hindu student is never set Islam.
    """
    from examinations.subjects import subject_plan
    from students.models import Enrollment, Student

    plan = subject_plan(academic_year, class_level)
    papers = list(subject.papers.all())
    enrollments = (
        Enrollment.objects.filter(
            academic_year=academic_year,
            section__in=sections,
            status=Enrollment.Status.ENROLLED,
            student__status=Student.Status.ACTIVE,
        )
        .select_related("student", "section")
        .prefetch_related("chosen_subjects")
        .order_by("section__name", "roll_number")
    )
    return [e for e in enrollments if _takes(e, subject, papers, plan)]


def answered(rows):
    """The part of a set of records that someone has already put something on."""
    return rows.filter(
        ~Q(status=Submission.Status.PENDING)
        | ~Q(source="")
        | Q(mark__isnull=False)
        | ~Q(grade="")
        | ~Q(feedback="")
        | Q(checked_at__isnull=False)
    )


def has_responses(task):
    return answered(task.submissions.all()).exists()


def materialise(task, selected=None):
    """Give every student expected to do the task a record, if they have none yet."""
    targets = {t.section_id: t for t in task.targets.select_related("section")}
    wanted = eligible(task.academic_year, task.class_level, task.subject, [t.section for t in targets.values()])
    if task.selected_only:
        chosen = (
            set(selected) if selected is not None else set(task.submissions.values_list("enrollment_id", flat=True))
        )
        wanted = [e for e in wanted if e.pk in chosen]
    have = set(task.submissions.values_list("enrollment_id", flat=True))
    Submission.objects.bulk_create(
        [
            Submission(school=task.school, task=task, target=targets[e.section_id], enrollment=e)
            for e in wanted
            if e.pk not in have
        ],
        ignore_conflicts=True,
    )


def sync_task(task, now=None):
    """
    Bring the task's list of students up to date in each section whose work is not yet due:
    add a student who has joined or changed group since it was set, and drop an untouched record
    of one who has left or moved. Once work is due the list stands, so a record is never lost.
    """
    now = now or timezone.now()
    if task.status == Task.Status.WITHDRAWN:
        return
    open_targets = {t.pk: t for t in task.targets.select_related("section") if t.due_at > now}
    if not open_targets:
        return
    wanted = {
        e.pk: e
        for e in eligible(
            task.academic_year, task.class_level, task.subject, [t.section for t in open_targets.values()]
        )
    }
    with transaction.atomic():
        rows = Submission.objects.filter(task=task, target_id__in=open_targets)
        untouched = rows.exclude(pk__in=answered(rows).values("pk"))
        gone = [
            row.pk
            for row in untouched.only("pk", "enrollment_id", "target_id")
            if row.enrollment_id not in wanted
            or wanted[row.enrollment_id].section_id != open_targets[row.target_id].section_id
        ]
        Submission.objects.filter(pk__in=gone).delete()
        if not task.selected_only:
            have = set(Submission.objects.filter(task=task).values_list("enrollment_id", flat=True))
            by_section = {t.section_id: t for t in open_targets.values()}
            Submission.objects.bulk_create(
                [
                    Submission(school=task.school, task=task, target=by_section[e.section_id], enrollment=e)
                    for e in wanted.values()
                    if e.pk not in have
                ],
                ignore_conflicts=True,
            )


def sync_student(enrollment, now=None):
    """Give a student records for open work in their section that was set before they joined it."""
    from examinations.subjects import subject_plan

    now = now or timezone.now()
    targets = (
        TaskSection.objects.filter(
            section_id=enrollment.section_id,
            due_at__gt=now,
            task__status=Task.Status.PUBLISHED,
            task__selected_only=False,
            task__academic_year_id=enrollment.academic_year_id,
        )
        .exclude(task__submissions__enrollment=enrollment)
        .select_related("task__subject")
    )
    targets = list(targets)
    if not targets:
        return
    plan = subject_plan(enrollment.academic_year, enrollment.class_level)
    Submission.objects.bulk_create(
        [
            Submission(school=target.school, task=target.task, target=target, enrollment=enrollment)
            for target in targets
            if _takes(enrollment, target.task.subject, list(target.task.subject.papers.all()), plan)
        ],
        ignore_conflicts=True,
    )


# ------------------------------------------------------------------ when it is due


def subject_family(subject):
    """The subject, the subject it is a paper of, and every paper of that subject."""
    from academics.models import Subject

    unit_id = subject.combines_into_id or subject.pk
    return {subject.pk, unit_id} | set(Subject.objects.filter(combines_into_id=unit_id).values_list("pk", flat=True))


def _lesson_starts(section, subject, academic_year):
    """Weekday -> the start of the section's first lesson in the subject that day."""
    from timetable.models import RoutineSlot

    own = {subject.pk} | set(subject.papers.values_list("pk", flat=True))
    # Its own lessons first; failing those, lessons of the rest of the subject (the other paper).
    for ids in (own, subject_family(subject)):
        starts = {}
        for weekday, start in RoutineSlot.objects.filter(
            school=section.school,
            academic_year=academic_year,
            section=section,
            subject_id__in=ids,
            period__is_active=True,
        ).values_list("weekday", "period__start_time"):
            if weekday not in starts or start < starts[weekday]:
                starts[weekday] = start
        if starts:
            return starts
    return {}


def _first_period(school):
    from timetable.models import Period

    period = Period.objects.filter(school=school, is_active=True, is_break=False).order_by("order").first()
    return period.start_time if period else DEFAULT_START


def suggest_due(section, subject, after, academic_year):
    """
    When work set at `after` should be due for a section: the start of the section's next
    lesson in the subject, on a school day. Weekends, holidays and the end of the year are
    skipped. Without the subject on the routine, the next school day. Returns (due_at, reason).
    """
    from holidays.models import holiday_dates_between

    school = section.school
    starts = _lesson_starts(section, subject, academic_year)
    first = timezone.localdate(after) + timedelta(days=1)
    last = first + timedelta(days=SUGGEST_DAYS)
    if academic_year is not None:
        last = min(last, academic_year.end_date)
    closed = holiday_dates_between(school, first, last) if first <= last else set()
    weekend = school.weekend_day_numbers
    next_school_day = None
    day = first
    while day <= last:
        if day.isoweekday() not in weekend and day not in closed:
            if day.isoweekday() in starts:
                return _at(day, starts[day.isoweekday()]), gettext("the next %(subject)s lesson") % {
                    "subject": subject.name
                }
            next_school_day = next_school_day or day
            if not starts:
                break
        day += timedelta(days=1)
    if next_school_day is None:
        return _at(first, _first_period(school)), gettext("no school day found: choose a date")
    if not starts:
        reason = gettext("the next school day: %(subject)s is not on the routine") % {"subject": subject.name}
    else:
        reason = gettext("the next school day")
    return _at(next_school_day, _first_period(school)), reason


# ------------------------------------------------------------------ how much is due


def limit_for(class_level):
    row = DailyLimit.objects.filter(school=class_level.school, class_level=class_level).first()
    return row.minutes if row else None


def school_week(school, day):
    """The school days of the week `day` falls in: from the day after the weekend."""
    weekend = school.weekend_day_numbers
    if weekend:
        start = day
        for _ in range(6):
            previous = start - timedelta(days=1)
            if previous.isoweekday() in weekend:
                break
            start = previous
    else:
        start = day - timedelta(days=day.isoweekday() - 1)
    return [start + timedelta(days=i) for i in range(7) if (start + timedelta(days=i)).isoweekday() not in weekend]


def section_load(section, days, *, exclude=None):
    """
    Each day's published homework due for a section: minutes for the whole section, and the
    tasks. Work set for chosen students only is listed but not added to the section's minutes.
    """
    targets = (
        TaskSection.objects.filter(
            school=section.school, section=section, due_on__in=days, task__status=Task.Status.PUBLISHED
        )
        .select_related("task__subject")
        .order_by("due_at")
    )
    if exclude is not None:
        targets = targets.exclude(task_id=exclude)
    by_day = {day: [] for day in days}
    for target in targets:
        by_day[target.due_on].append(target.task)
    limit = limit_for(section.class_level)
    loads = []
    for day in days:
        tasks = by_day[day]
        minutes = sum(task.estimated_minutes for task in tasks if not task.selected_only)
        loads.append(
            {
                "day": day,
                "tasks": tasks,
                "minutes": minutes,
                "limit": limit,
                "over": limit is not None and minutes > limit,
            }
        )
    return loads


def load_with(section, due_at, minutes, *, exclude=None, whole_section=True):
    """
    The school week around a due date, with this task's minutes added on its day: what the
    teacher sees before setting the work. `over` on the due day is the warning.
    """
    day = timezone.localdate(due_at)
    days = school_week(section.school, day)
    if day not in days:
        days = sorted([*days, day])
    loads = section_load(section, days, exclude=exclude)
    for load in loads:
        load["this"] = load["day"] == day
        if load["this"] and whole_section:
            load["minutes"] += minutes
            load["over"] = load["limit"] is not None and load["minutes"] > load["limit"]
    return {"section": section, "day": day, "days": loads, "over": any(ld["this"] and ld["over"] for ld in loads)}


# ------------------------------------------------------------------ setting work


def _clean_fields(task):
    if not (task.title or "").strip():
        raise ValidationError(gettext("Give the task a title."))
    if not task.estimated_minutes or not 1 <= task.estimated_minutes <= 300:
        raise ValidationError(gettext("The time needed must be from 1 to 300 minutes."))
    if task.marking == Task.Marking.MARKS:
        if not task.max_marks or task.max_marks <= 0:
            raise ValidationError(gettext("Say what the work is marked out of."))
    else:
        task.max_marks = None


@transaction.atomic
def save_task(*, user, task, targets, action, selected=None, publish_at=None, now=None):
    """
    Save a task and the sections it is set for, and give each student expected to do it a record.

    `targets` is {section: due_at}. `selected` is a list of enrollment ids when the work is for
    chosen students only, else None. `action`:
    - "draft": kept to its author (and the managers) until published;
    - "publish": out now;
    - "schedule": out at `publish_at`;
    - "save": change a task already published, keeping when it goes out.
    """
    now = now or timezone.now()
    school = task.school
    sections = list(targets)
    if not sections:
        raise ValidationError(gettext("Choose at least one section."))
    for section in sections:
        if section.school_id != school.pk or section.class_level_id != task.class_level_id:
            raise PermissionDenied("That section is not in this class.")
    if not may_set(user, school, task.subject, sections, task.academic_year):
        raise PermissionDenied("You do not teach this subject in every section chosen.")

    existing = Task.objects.select_for_update().get(pk=task.pk) if task.pk else None
    if existing is not None:
        if not may_manage(user, existing):
            raise PermissionDenied
        if existing.status == Task.Status.WITHDRAWN:
            raise ValidationError(gettext("A withdrawn task cannot be changed."))
        if any(getattr(existing, name) != getattr(task, name) for name in LOCKED_FIELDS) and has_responses(existing):
            raise ValidationError(
                gettext(
                    "Work has already been recorded for this task, so its subject, marking and who it is for "
                    "cannot change. Set a new task instead."
                )
            )
    task.selected_only = selected is not None
    _clean_fields(task)

    if action == "draft":
        if existing is not None and existing.status != Task.Status.DRAFT:
            raise ValidationError(gettext("A published task cannot go back to being a draft; withdraw it instead."))
        task.status, task.publish_at = Task.Status.DRAFT, None
    elif action == "publish":
        if existing is not None and existing.is_live(now):
            task.publish_at = existing.publish_at
        else:
            task.publish_at = now
        task.status = Task.Status.PUBLISHED
    elif action == "schedule":
        if existing is not None and existing.is_live(now):
            raise ValidationError(gettext("This task is already out, so it cannot be scheduled."))
        if publish_at is None or publish_at <= now:
            raise ValidationError(gettext("Choose a time in the future to publish it."))
        task.status, task.publish_at = Task.Status.PUBLISHED, publish_at
    elif action == "save":
        if existing is None or existing.status != Task.Status.PUBLISHED:
            raise ValidationError(gettext("Only a published task can be saved this way."))
        task.status, task.publish_at = existing.status, existing.publish_at
    else:
        raise ValidationError("Unknown action.")

    if task.status == Task.Status.PUBLISHED:
        for section, due_at in targets.items():
            if due_at <= task.publish_at:
                raise ValidationError(
                    gettext("%(section)s: the due time must come after the work goes out.") % {"section": section}
                )

    if selected is not None:
        chosen = set(selected)
        allowed = {e.pk for e in eligible(task.academic_year, task.class_level, task.subject, sections)}
        if not chosen:
            raise ValidationError(gettext("Choose the students this work is for."))
        if chosen - allowed:
            raise ValidationError(gettext("Some of the students chosen do not take this subject in these sections."))

    if task.teacher_id is None:
        task.teacher = employee_of(user)
    if task.set_by_id is None:
        task.set_by = user
    task.full_clean()
    task.save()

    current = {target.section_id: target for target in task.targets.all()}
    wanted = {section.pk for section in sections}
    for section_id, target in current.items():
        if section_id in wanted:
            continue
        if answered(target.submissions.all()).exists():
            raise ValidationError(
                gettext("%(section)s: work has already been recorded there, so it cannot be taken off.")
                % {"section": target.section}
            )
        target.delete()
    for section, due_at in targets.items():
        target = current.get(section.pk)
        if target is None:
            TaskSection.objects.create(school=school, task=task, section=section, due_at=due_at)
        elif target.due_at != due_at:
            target.due_at = due_at
            target.save()
            # Work handed in at home is late or on time against the new due time.
            for row in target.submissions.filter(source=Submission.Source.FAMILY, extended_to__isnull=True):
                late = row.handed_in_at is not None and row.handed_in_at > due_at
                if row.late != late:
                    Submission.objects.filter(pk=row.pk).update(late=late, version=row.version + 1)

    if selected is not None and existing is not None:
        dropped = task.submissions.exclude(enrollment_id__in=selected)
        dropped.exclude(pk__in=answered(dropped).values("pk")).delete()
    materialise(task, selected)

    if existing is None:
        _audit(task, user, "homework.task_created", f"{task.title} ({task.get_status_display()})")
    else:
        _audit(task, user, "homework.task_changed", f"{task.title} ({task.get_status_display()})")
    return task


def withdraw(*, user, task, now=None):
    if not may_manage(user, task):
        raise PermissionDenied
    if task.status != Task.Status.PUBLISHED:
        raise ValidationError(gettext("Only a published task can be withdrawn."))
    task.status = Task.Status.WITHDRAWN
    task.save(update_fields=["status", "updated_at"])
    _audit(task, user, "homework.task_withdrawn", task.title)


def delete_task(*, user, task):
    if not may_manage(user, task):
        raise PermissionDenied
    if task.status != Task.Status.DRAFT and has_responses(task):
        raise ValidationError(
            gettext("Work has already been recorded for this task, so it cannot be deleted. Withdraw it instead.")
        )
    _audit(task, user, "homework.task_deleted", task.title)
    task.delete()


# ------------------------------------------------------------------ recording what was done


def family_mark_done(*, user, submission, done, now=None):
    """
    A student, or their guardian on the family's phone, ticks work done at home or takes the
    tick back. Once the teacher has checked the work, the teacher's record stands.
    """
    now = now or timezone.now()
    with transaction.atomic():
        row = (
            Submission.objects.select_for_update()
            .select_related("task", "target", "enrollment__student")
            .get(pk=submission.pk)
        )
        if not row.task.is_live(now):
            raise ValidationError(gettext("This work is not open."))
        if row.task.hand_in == Task.HandIn.ONLINE:
            raise ValidationError(gettext("This work is handed in online."))
        if row.checked_at is not None:
            raise ValidationError(gettext("Your teacher has already checked this work, so it cannot be changed here."))
        if done:
            if row.status != Submission.Status.PENDING:
                return row
            row.status, row.source = Submission.Status.DONE, Submission.Source.FAMILY
            row.handed_in_at, row.handed_in_by = now, user
            row.by_guardian = row.enrollment.student.user_id != user.pk
            row.late = now > row.due_at
        else:
            if row.source != Submission.Source.FAMILY:
                return row
            row.status, row.source = Submission.Status.PENDING, Submission.Source.NONE
            row.handed_in_at, row.handed_in_by, row.by_guardian, row.late = None, None, False, False
        row.version += 1
        row.save()
    return row


def check_rows(*, user, task, target, rows, return_work, now=None):
    """
    Save a teacher's check of one section's work, all or nothing.

    `rows` is a list of dicts: id, version (as the teacher saw it), status, late, mark, grade,
    feedback, reason. A row someone else changed meanwhile, or a mark out of range, and nothing
    is saved. Giving a mark or grade to work not yet checked records it as done. With
    `return_work`, checked work is returned: the family sees the feedback, and the mark if the
    task shows marks.
    """
    now = now or timezone.now()
    if target.task_id != task.pk or not may_mark(user, task, target.section):
        raise PermissionDenied
    if task.status != Task.Status.PUBLISHED:
        raise ValidationError(gettext("Only published work can be checked."))
    statuses = set(Submission.Status.values)
    with transaction.atomic():
        current = {
            row.pk: row
            for row in Submission.objects.select_for_update()
            .filter(target=target)
            .select_related("enrollment__student")
        }
        stale, problems, changed = [], [], {}
        for given in rows:
            row = current.get(given["id"])
            if row is None:
                raise PermissionDenied("That record is not in this section.")
            name = row.enrollment.student.full_name
            if row.version != given["version"]:
                stale.append(name)
                continue
            status = given["status"]
            if status not in statuses:
                problems.append(gettext("%(name)s: choose a status.") % {"name": name})
                continue
            mark = given.get("mark")
            if mark is not None:
                if task.marking != Task.Marking.MARKS:
                    problems.append(gettext("%(name)s: this work is not marked out of a number.") % {"name": name})
                elif mark < 0 or mark > task.max_marks:
                    problems.append(
                        gettext("%(name)s: give a mark from 0 to %(max)s.") % {"name": name, "max": task.max_marks}
                    )
            grade = (given.get("grade") or "").strip()
            feedback = (given.get("feedback") or "").strip()
            reason = (given.get("reason") or "").strip()
            if len(grade) > 10:
                problems.append(gettext("%(name)s: keep a grade to 10 characters.") % {"name": name})
            if len(feedback) > 1000:
                problems.append(gettext("%(name)s: keep feedback to 1000 characters.") % {"name": name})
            if (mark is not None or grade) and status == Submission.Status.PENDING:
                status = Submission.Status.DONE
            new = {
                "status": status,
                "late": bool(given.get("late")) and status in Submission.HANDED_IN,
                "mark": mark,
                "grade": grade,
                "feedback": feedback,
                "reason": reason[:150] if status == Submission.Status.EXCUSED else "",
            }
            if any(getattr(row, field) != value for field, value in new.items()):
                if new["status"] != row.status:
                    row.source = (
                        Submission.Source.NONE if status == Submission.Status.PENDING else Submission.Source.TEACHER
                    )
                for field, value in new.items():
                    setattr(row, field, value)
                if status == Submission.Status.PENDING and not (mark is not None or grade or feedback):
                    # Put back to "not checked yet": the family may tick it again.
                    row.checked_by, row.checked_at, row.returned_at = None, None, None
                else:
                    row.checked_by, row.checked_at = user, now
                changed[row.pk] = row
            if new["status"] in Submission.HANDED_IN:
                row.redo_requested = False
            if return_work and row.status != Submission.Status.PENDING and not is_returned(row):
                row.returned_at = now
                changed[row.pk] = row
        if stale:
            raise ValidationError(
                gettext(
                    "Someone else changed this work while you had it open: %(names)s. Nothing was saved; "
                    "open the page again and check once more."
                )
                % {"names": ", ".join(stale)}
            )
        if problems:
            raise ValidationError(problems)
        for row in changed.values():
            row.version += 1
            row.save()
        if changed:
            _audit(task, user, "homework.checked", f"{target.section}: {len(changed)} record(s)")
    return len(changed)


# ------------------------------------------------------------------ handing in online


def is_returned(row):
    """The teacher has given the work back since it was last handed in."""
    return row.returned_at is not None and (row.handed_in_at is None or row.returned_at >= row.handed_in_at)


def hand_in_closed(row, now):
    """Why the family cannot hand in or change the hand-in now, as a sentence; None when they can."""
    task = row.task
    if not task.is_live(now) or task.hand_in != Task.HandIn.ONLINE:
        return gettext("This work is not handed in online.")
    if row.status == Submission.Status.EXCUSED:
        return gettext("You have been excused from this work.")
    if row.redo_requested:
        return None
    if is_returned(row):
        return gettext("Your teacher has returned this work.")
    if now > row.due_at and not task.allow_late:
        return gettext("The due time has passed, and this work is not taken late.")
    return None


def _family_row(submission):
    return (
        Submission.objects.select_for_update()
        .select_related("task", "target", "enrollment__student")
        .get(pk=submission.pk)
    )


def hand_in_online(*, user, submission, uploads, note="", now=None):
    """
    A student, or a guardian with the family's phone, hands work in: photos of the pages, a
    PDF or a Word document, and a short note. More pages can be added until it is returned.
    """
    from .files import prepare_all

    now = now or timezone.now()
    with transaction.atomic():
        row = _family_row(submission)
        problem = hand_in_closed(row, now)
        if problem:
            raise ValidationError(problem)
        current = list(row.files.filter(attempt=row.attempt))
        prepared = prepare_all(uploads, already=len(current), already_bytes=sum(f.size for f in current))
        note = (note or "").strip()
        if len(note) > 500:
            raise ValidationError(gettext("Keep the note to 500 characters."))
        if not prepared and not current and not note and not row.note:
            raise ValidationError(gettext("Add a photo or a file of the work, or write a note."))
        page = max((f.page for f in current), default=0)
        for content, kind in prepared:
            page += 1
            SubmissionFile.objects.create(
                school=row.school,
                submission=row,
                file=content,
                kind=kind,
                size=content.size,
                page=page,
                attempt=row.attempt,
                uploaded_by=user,
            )
        row.status, row.source = Submission.Status.DONE, Submission.Source.ONLINE
        row.handed_in_at, row.handed_in_by = now, user
        row.by_guardian = row.enrollment.student.user_id != user.pk
        row.late = now > row.due_at
        if note:
            row.note = note
        row.redo_requested = False
        row.version += 1
        row.save()
    return row


def remove_page(*, user, page, now=None):
    """Take a page back out of a hand-in that has not been returned."""
    now = now or timezone.now()
    with transaction.atomic():
        row = _family_row(page.submission)
        problem = hand_in_closed(row, now)
        if problem:
            raise ValidationError(problem)
        if page.attempt != row.attempt:
            raise ValidationError(gettext("Only pages of the latest hand-in can be taken out."))
        page.delete()
        if not row.files.filter(attempt=row.attempt).exists() and not row.note:
            row.status, row.source = Submission.Status.PENDING, Submission.Source.NONE
            row.handed_in_at, row.handed_in_by, row.by_guardian, row.late = None, None, False, False
        row.version += 1
        row.save()
    return row


# ------------------------------------------------------------------ the teacher's answer


def _teacher_row(user, submission):
    row = (
        Submission.objects.select_for_update()
        .select_related("task", "target__section", "enrollment__student")
        .get(pk=submission.pk)
    )
    if not may_mark(user, row.task, row.target.section):
        raise PermissionDenied
    if row.task.status != Task.Status.PUBLISHED:
        raise ValidationError(gettext("Only published work can be checked."))
    return row


def _later(extend_to, now):
    if extend_to is not None and extend_to <= now:
        raise ValidationError(gettext("Choose a new due time in the future."))
    return extend_to


def ask_to_redo(*, user, submission, feedback="", extend_to=None, now=None):
    """Give work back to be done again, saying why; optionally with more time."""
    now = now or timezone.now()
    with transaction.atomic():
        row = _teacher_row(user, submission)
        feedback = (feedback or "").strip()
        if len(feedback) > 1000:
            raise ValidationError(gettext("Keep feedback to 1000 characters."))
        if not feedback:
            raise ValidationError(gettext("Say what needs doing again."))
        row.extended_to = _later(extend_to, now) or row.extended_to
        row.feedback = feedback
        row.status, row.source, row.late = Submission.Status.PENDING, Submission.Source.NONE, False
        row.redo_requested = True
        row.attempt += 1
        row.returned_at = now
        row.checked_by, row.checked_at = None, None
        row.version += 1
        row.save()
        _audit(row.task, user, "homework.redo_requested", row.enrollment.student.student_id)
    return row


def give_more_time(*, user, submission, extend_to, now=None):
    """A later due time for one student: a catch-up after absence, or a fair extension."""
    now = now or timezone.now()
    with transaction.atomic():
        row = _teacher_row(user, submission)
        if extend_to is None:
            raise ValidationError(gettext("Choose the new due time."))
        row.extended_to = _later(extend_to, now)
        if row.status in (Submission.Status.NOT_DONE, Submission.Status.ABSENT):
            row.status, row.source = Submission.Status.PENDING, Submission.Source.NONE
            row.checked_by, row.checked_at = None, None
        if row.handed_in_at is not None:
            row.late = row.handed_in_at > row.extended_to
        row.version += 1
        row.save()
        _audit(row.task, user, "homework.extended", row.enrollment.student.student_id)
    return row


def excuse(*, user, submission, reason, now=None):
    """Excuse a student from the work. Excused work is left out of every figure."""
    now = now or timezone.now()
    with transaction.atomic():
        row = _teacher_row(user, submission)
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError(gettext("Say why the student is excused."))
        row.status, row.reason, row.late = Submission.Status.EXCUSED, reason[:150], False
        row.redo_requested = False
        row.checked_by, row.checked_at = user, now
        row.version += 1
        row.save()
        _audit(row.task, user, "homework.excused", row.enrollment.student.student_id)
    return row


# ------------------------------------------------------------------ worksheets and links


def clean_link(url):
    from django.core.validators import URLValidator

    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    try:
        URLValidator(schemes=["http", "https"])(url)
    except ValidationError:
        raise ValidationError(gettext("That link is not a web address.")) from None
    return url


def add_resources(*, user, task, prepared=(), link="", title=""):
    """
    Give the task worksheets (checked by files.prepare, each with its name) and a link.
    Families see them with the task.
    """
    from pathlib import PurePath

    if not may_manage(user, task):
        raise PermissionDenied
    for (content, kind), name in prepared:
        TaskResource.objects.create(
            school=task.school,
            task=task,
            title=(PurePath(name).stem or gettext("Worksheet"))[:150],
            file=content,
            kind=kind,
            size=content.size,
        )
    link = clean_link(link)
    if link:
        TaskResource.objects.create(school=task.school, task=task, title=(title or link)[:150], url=link)


def remove_resources(*, user, task, ids):
    if not may_manage(user, task):
        raise PermissionDenied
    for resource in task.resources.filter(pk__in=ids):
        resource.delete()
