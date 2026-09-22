from core.crud import crud
from .models import Employee
app_name = "employees"
urlpatterns = crud(Employee, "employees", "",
    ["employee_id","user","employee_type","first_name","last_name","gender","date_of_birth","phone","email","nid",
     "blood_group","religion","photo","address","department","designation","qualification","joining_date","basic_salary","status"],
    [("ID","employee_id"),("Name","full_name"),("Type","employee_type"),("Department","department"),("Phone","phone"),("Status","status","badge")],
    prefix="", search=("first_name","last_name","employee_id"),
    actions=(("Teaching assignments","settings:subject_teacher_list"),("Departments","settings:department_list"),("Designations","settings:designation_list")))
