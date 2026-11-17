// 总分对账页：触发版本化归档。同批示数重放返回既有版本，不新增电量或费用。
function notice(message, isError = false) {
  const box = document.getElementById("notice");
  if (!box) return;
  box.textContent = message;
  box.classList.toggle("is-error", Boolean(isError));
  box.hidden = false;
}

const button = document.getElementById("park-archive-button");
if (button) {
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    const label = button.textContent;
    button.textContent = "归档中";
    try {
      const response = await fetch("/api/park/archive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error((payload.error || response.status) + " " + (payload.message || ""));
      }
      notice("归档完成：新增 " + payload.created + " 版，命中既有 " + payload.reused + " 版");
      window.setTimeout(() => window.location.reload(), 600);
    } catch (error) {
      notice(String(error.message || error), true);
    } finally {
      button.textContent = label;
      button.disabled = false;
    }
  });
}
