// Handing homework in from a phone. Each photo is made smaller in the browser before it is
// sent (at most 1600 pixels on its longest side, as JPEG), so a page costs a few hundred KB of
// data rather than several MB. Pages can be put in order or taken out before sending, and the
// upload shows its progress. Without this script the form still works as a plain upload.
(() => {
  const form = document.querySelector("form[data-hand-in]");
  if (!form || !window.FormData || !window.XMLHttpRequest || !window.URL) return;
  const inputs = Array.from(form.querySelectorAll("input[type=file][data-pages]"));
  const list = form.querySelector("[data-thumbs]");
  const progress = form.querySelector("[data-progress]");
  const bar = progress.querySelector("progress");
  const text = progress.querySelector("[data-progress-text]");
  const button = form.querySelector("button:not([type=button])");
  const LONGEST = 1600;
  const QUALITY = 0.7;
  const pages = [];

  async function shrink(file) {
    if (!file.type.startsWith("image/") || !window.createImageBitmap) return file;
    try {
      const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
      const scale = Math.min(1, LONGEST / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(bitmap.width * scale);
      canvas.height = Math.round(bitmap.height * scale);
      const context = canvas.getContext("2d");
      context.fillStyle = "#fff";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", QUALITY));
      if (!blob || blob.size >= file.size) return file;
      const name = (file.name || "page").replace(/\.[^.]+$/, "") + ".jpg";
      return new File([blob], name, { type: "image/jpeg" });
    } catch (e) {
      // A photo this browser cannot read (HEIC on some phones) goes as it is; the school says if it cannot use it.
      return file;
    }
  }

  function button_(label, action) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.className = "px-1 text-slate-600 hover:text-slate-900";
    b.addEventListener("click", action);
    return b;
  }

  function render() {
    list.innerHTML = "";
    pages.forEach((page, index) => {
      const item = document.createElement("li");
      item.className = "rounded-md border border-slate-200 p-1 text-center text-xs";
      if (page.url) {
        const img = document.createElement("img");
        img.src = page.url;
        img.alt = String(index + 1);
        img.className = "mx-auto h-24 w-full object-cover";
        item.appendChild(img);
      } else {
        const name = document.createElement("span");
        name.className = "block truncate py-8";
        name.textContent = "📄 " + page.file.name;
        item.appendChild(name);
      }
      const tools = document.createElement("div");
      tools.className = "flex items-center justify-center gap-1";
      tools.appendChild(document.createTextNode(String(index + 1)));
      if (index > 0) tools.appendChild(button_("◀", () => { pages.splice(index - 1, 0, pages.splice(index, 1)[0]); render(); }));
      if (index < pages.length - 1) tools.appendChild(button_("▶", () => { pages.splice(index + 1, 0, pages.splice(index, 1)[0]); render(); }));
      tools.appendChild(button_("✕", () => { const [gone] = pages.splice(index, 1); if (gone.url) URL.revokeObjectURL(gone.url); render(); }));
      item.appendChild(tools);
      list.appendChild(item);
    });
  }

  inputs.forEach((input) => {
    input.addEventListener("change", async () => {
      const chosen = Array.from(input.files || []);
      input.value = "";
      button.disabled = true;
      for (const file of chosen) {
        const small = await shrink(file);
        pages.push({ file: small, url: small.type.startsWith("image/") ? URL.createObjectURL(small) : "" });
      }
      button.disabled = false;
      render();
    });
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(form);
    data.delete("pages");
    pages.forEach((page) => data.append("pages", page.file, page.file.name));
    const request = new XMLHttpRequest();
    // A field named "action" hides form.action, so read the attribute.
    request.open("POST", form.getAttribute("action") || window.location.href);
    request.setRequestHeader("X-Requested-With", "fetch");
    request.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) bar.value = Math.round((e.loaded / e.total) * 100);
    });
    request.addEventListener("load", () => {
      let answer = {};
      try { answer = JSON.parse(request.responseText); } catch (e) { /* not JSON: show the page */ }
      if (request.status === 200 && answer.ok) {
        window.location.reload();
        return;
      }
      button.disabled = false;
      text.textContent = (answer.errors || [form.dataset.failed]).join(" ");
      text.className = "text-red-700";
    });
    request.addEventListener("error", () => {
      button.disabled = false;
      text.textContent = form.dataset.failed;
      text.className = "text-red-700";
    });
    button.disabled = true;
    progress.classList.remove("hidden");
    text.textContent = "";
    request.send(data);
  });
})();
