"""
The admissions funnel: for each class of a round, how many applied, were assessed, were
offered a place, accepted and joined, against the seats; and where families heard of the school.

It counts cleared applications too. Clearing keeps the status an application reached and how the
family heard of the school, and nothing that identifies anyone, so the report still adds up
long after the details have gone.
"""

from collections import Counter

from .models import Application

S = Application.Status
OFFERED = [S.OFFERED, S.LAPSED, S.ACCEPTED, S.OFFER_DECLINED, S.ENROLLED]
ACCEPTED = [S.ACCEPTED, S.ENROLLED]


def funnel(admission_round):
    rows = []
    for row in admission_round.classes.select_related("class_level"):
        applications = Application.objects.filter(round_class=row)
        statuses = Counter(applications.values_list("status", flat=True))
        applied = sum(statuses.values())
        offered = sum(statuses[status] for status in OFFERED)
        enrolled = statuses[S.ENROLLED]
        rows.append(
            {
                "class": row.class_level,
                "seats": row.seats,
                "applied": applied,
                "online": applications.filter(channel=Application.Channel.ONLINE).count(),
                "assessed": applications.filter(assessment_results__attended=True).distinct().count(),
                "offered": offered,
                "accepted": sum(statuses[status] for status in ACCEPTED),
                "enrolled": enrolled,
                "waiting": statuses[S.WAITLISTED],
                "filled": round(100 * enrolled / row.seats) if row.seats else None,
                "per_seat": round(applied / row.seats, 1) if row.seats else None,
            }
        )
    heard = Counter(
        Application.objects.filter(round_class__admission_round=admission_round).values_list("heard_from", flat=True)
    )
    labels = dict(Application.HeardFrom.choices)
    sources = sorted(
        ((str(labels.get(key, "Not said")), count) for key, count in heard.items()), key=lambda pair: -pair[1]
    )
    return rows, sources


HEADERS = [
    "Class",
    "Seats",
    "Applied",
    "Applied online",
    "Assessed",
    "Offered a place",
    "Accepted",
    "Enrolled",
    "On the waiting list",
    "Seats filled (%)",
    "Applications per seat",
]


def table(rows):
    return [
        [
            str(row["class"]),
            row["seats"],
            row["applied"],
            row["online"],
            row["assessed"],
            row["offered"],
            row["accepted"],
            row["enrolled"],
            row["waiting"],
            row["filled"],
            row["per_seat"],
        ]
        for row in rows
    ]
