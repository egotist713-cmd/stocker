// Stocker UI: подтверждения действий (без фреймворков, без сети).
document.addEventListener("DOMContentLoaded", () => {
  const approve = document.querySelector("form.approve");
  if (approve) {
    approve.addEventListener("submit", (event) => {
      const attest = approve.querySelector("#attest");
      if (attest && attest.checked &&
          !confirm("Подтвердите: люди в кадре не узнаваемы. Решение будет записано как ваша аттестация.")) {
        event.preventDefault();
      }
    });
  }
  for (const form of document.querySelectorAll("form.reject")) {
    form.addEventListener("submit", (event) => {
      const reason = form.querySelector("input[name=reason]");
      if (!reason.value.trim()) { event.preventDefault(); reason.focus(); return; }
      if (!confirm("Отклонить объект? Это решение человека, отменить его нельзя.")) event.preventDefault();
    });
  }
  for (const form of document.querySelectorAll("form.return")) {
    form.addEventListener("submit", (event) => {
      if (!confirm("Вернуть объект на проверку?")) event.preventDefault();
    });
  }
  for (const form of document.querySelectorAll("form.attest-form")) {
    form.addEventListener("submit", (event) => {
      if (!confirm("Подтвердите: люди в кадре не узнаваемы.")) event.preventDefault();
    });
  }
});
