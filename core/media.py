from django.http import FileResponse,Http404
from django.shortcuts import get_object_or_404
from .access import require_permission,students_for,is_manager

@require_permission(None)
def image(request,path):
    # Only explicitly authorized image model fields are served. Documents have dedicated endpoints.
    from core.models import School
    from students.models import Student
    from employees.models import Employee
    from users.models import User
    obj=None
    field=None
    candidates=[
        (School.objects.filter(pk=request.school.pk),"logo"),
        (students_for(request.user,request.school),"photo"),
        (User.objects.filter(pk=request.user.pk),"avatar"),
    ]
    if request.user.has_perm("employees.view_employee"):
        candidates.append((Employee.objects.filter(school=request.school),"photo"))
    for qs,name in candidates:
        obj=qs.filter(**{name:path}).first()
        if obj is not None:
            field=getattr(obj,name)
            break
    if not field:
        raise Http404
    try:
        response=FileResponse(field.open("rb"))
    except FileNotFoundError:
        raise Http404
    response["X-Content-Type-Options"]="nosniff"
    return response
