document.addEventListener("DOMContentLoaded", () => {
  const userSelect = document.getElementById("id_user");
  const dataElement = document.getElementById("employee-login-data");
  if (!userSelect || !dataElement) return;

  const users = JSON.parse(dataElement.textContent);
  const createLogin = document.getElementById("id_create_login");
  const employeeType = document.getElementById("id_employee_type");

  const update = (fillDetails) => {
    const user = users[userSelect.value];
    if (createLogin) {
      createLogin.disabled = Boolean(user);
      createLogin.checked = !user;
    }
    if (!user || !fillDetails) return;
    for (const field of ["first_name", "last_name", "email", "phone"]) {
      const input = document.getElementById(`id_${field}`);
      if (input) input.value = user[field] || "";
    }
    if (employeeType) {
      if (user.roles.includes("Teacher")) employeeType.value = "teacher";
      else if (user.roles.includes("Staff")) employeeType.value = "staff";
    }
  };

  userSelect.addEventListener("change", () => update(true));
  update(false);
});
