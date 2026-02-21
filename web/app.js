(() => {

  const page = document.body.dataset.page;
  if (page === "input") {
    initInputPage();
  } else if (page === "canvas") {
    initCanvasPage();
  }

  function $(id) {
    return document.getElementById(id);
  }

  function parseErrorMessage(text) {
    try {
      const obj = JSON.parse(text);
      if (obj.error) return obj.error;
      if (obj.detail) {
        if (typeof obj.detail === "string") return obj.detail;
        return "请求参数有误，请检查输入内容。";
      }
    } catch (_) {}
    if (text.length > 200) return "请求失败，请检查输入或稍后重试。";
    return text;
  }

  function initInputPage() {
    const confirmBtn = $("confirmBtn");
    const errorMsg = $("errorMsg");
    const uploadZone = $("uploadZone");
    const referenceFile = $("referenceFile");
    const referencePreview = $("referencePreview");
    const referenceStatus = $("referenceStatus");

    if (uploadZone && referenceFile) {
      uploadZone.addEventListener("click", () => referenceFile.click());
      uploadZone.addEventListener("dragover", (event) => {
        event.preventDefault();
        uploadZone.classList.add("dragging");
      });
      uploadZone.addEventListener("dragleave", () => {
        uploadZone.classList.remove("dragging");
      });
      uploadZone.addEventListener("drop", (event) => {
        event.preventDefault();
        uploadZone.classList.remove("dragging");
        const file = event.dataTransfer.files[0];
        if (file) {
          uploadReference(file, confirmBtn, referencePreview, referenceStatus);
        }
      });
      referenceFile.addEventListener("change", () => {
        const file = referenceFile.files[0];
        if (file) {
          uploadReference(file, confirmBtn, referencePreview, referenceStatus);
        }
      });
    }

    confirmBtn.addEventListener("click", async () => {
      errorMsg.textContent = "";
      const methodText = $("methodText").value.trim();
      if (!methodText) {
        errorMsg.textContent = "请输入论文方法描述文本。";
        return;
      }

      confirmBtn.disabled = true;
      confirmBtn.textContent = "正在启动...";

      const payload = {
        method_text: methodText,
        provider: $("provider").value,
        api_key: $("apiKey").value.trim() || null,
        optimize_iterations: parseInt($("optimizeIterations").value, 10),
        reference_image_path: $("referenceImage").value.trim() || null,
        sam_backend: $("samBackend").value,
        sam_api_key: $("samApiKey").value.trim() || null,
      };

      try {
        const response = await fetch("/api/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

        if (!response.ok) {
          const text = await response.text();
          throw new Error(parseErrorMessage(text || "请求失败"));
        }

        const data = await response.json();
        window.location.href = `/canvas.html?job=${encodeURIComponent(data.job_id)}`;
      } catch (err) {
        errorMsg.textContent = err.message || "启动任务失败";
        confirmBtn.disabled = false;
        confirmBtn.textContent = "开始生成";
      }
    });
  }

  async function uploadReference(file, confirmBtn, previewEl, statusEl) {
    if (!file.type.startsWith("image/")) {
      statusEl.textContent = "仅支持图片文件。";
      return;
    }

    confirmBtn.disabled = true;
    statusEl.textContent = "正在上传参考图...";

    const formData = new FormData();
    formData.append("file", file);

    try {
      const response = await fetch("/api/upload", {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const text = await response.text();
        throw new Error(parseErrorMessage(text || "上传失败"));
      }

      const data = await response.json();
      const referenceInput = $("referenceImage");
      if (referenceInput) {
        referenceInput.value = data.path;
      }
      statusEl.textContent = `已使用上传的参考图：${data.name}`;
      if (previewEl) {
        previewEl.src = data.url || "";
        previewEl.classList.add("visible");
      }
    } catch (err) {
      statusEl.textContent = err.message || "上传失败";
    } finally {
      confirmBtn.disabled = false;
    }
  }

  async function initCanvasPage() {
    const params = new URLSearchParams(window.location.search);
    const jobId = params.get("job");
    const statusText = $("statusText");
    const jobIdEl = $("jobId");
    const artifactPanel = $("artifactPanel");
    const artifactList = $("artifactList");
    const toggle = $("artifactToggle");
    const logToggle = $("logToggle");
    const logPanel = $("logPanel");
    const logBody = $("logBody");
    const iframe = $("svgEditorFrame");
    const fallback = $("svgFallback");
    const fallbackObject = $("fallbackObject");
    const cancelBtn = $("cancelBtn");
    const progressFill = $("progressFill");
    const progressSteps = $("progressSteps");
    const previewOverlay = $("previewOverlay");
    const previewImage = $("previewImage");
    const previewStepLabel = $("previewStepLabel");

    if (!jobId) {
      statusText.textContent = "缺少任务 ID";
      return;
    }

    jobIdEl.textContent = jobId;

    toggle.addEventListener("click", () => {
      artifactPanel.classList.toggle("open");
    });

    logToggle.addEventListener("click", () => {
      logPanel.classList.toggle("open");
    });

    // 取消任务
    cancelBtn.addEventListener("click", async () => {
      if (!confirm("确定要取消当前任务吗？")) return;
      try {
        await fetch(`/api/cancel/${jobId}`, { method: "POST" });
        statusText.textContent = "已取消";
        cancelBtn.style.display = "none";
      } catch (_) {}
    });

    let svgEditAvailable = false;
    let svgEditPath = null;
    try {
      const configRes = await fetch("/api/config");
      if (configRes.ok) {
        const config = await configRes.json();
        svgEditAvailable = Boolean(config.svgEditAvailable);
        svgEditPath = config.svgEditPath || null;
      }
    } catch (err) {
      svgEditAvailable = false;
    }

    if (svgEditAvailable && svgEditPath) {
      iframe.src = svgEditPath;
    } else {
      fallback.classList.add("active");
      iframe.style.display = "none";
    }

    let svgReady = false;
    let pendingSvgText = null;

    iframe.addEventListener("load", () => {
      svgReady = true;
      if (pendingSvgText) {
        tryLoadSvg(pendingSvgText);
        pendingSvgText = null;
      }
    });

    const stepMap = {
      figure: { step: 1, label: "图片已生成" },
      samed: { step: 2, label: "SAM3 分割完成" },
      icon_raw: { step: 3, label: "图标已提取" },
      icon_nobg: { step: 3, label: "图标已去背景" },
      template_svg: { step: 4, label: "模板 SVG 已就绪" },
      optimized_svg: { step: 4, label: "优化模板已就绪" },
      final_svg: { step: 5, label: "最终 SVG 已就绪" },
    };

    const previewLabels = {
      0: "正在生成图片...",
      1: "正在进行 SAM3 分割...",
      2: "正在提取图标...",
      3: "正在生成 SVG 模板...",
      4: "正在最终合成...",
    };

    let currentStep = 0;

    function updateProgress(step) {
      currentStep = step;
      const pct = Math.round((step / 5) * 100);
      progressFill.style.width = pct + "%";

      const steps = progressSteps.querySelectorAll(".progress-step");
      steps.forEach((el) => {
        const s = parseInt(el.dataset.step, 10);
        el.classList.toggle("done", s < step);
        el.classList.toggle("active", s === step);
      });

      if (previewLabels[step] !== undefined) {
        previewStepLabel.textContent = previewLabels[step];
      }
    }

    const artifacts = new Set();
    const eventSource = new EventSource(`/api/events/${jobId}`);
    let isFinished = false;

    eventSource.addEventListener("artifact", async (event) => {
      const data = JSON.parse(event.data);
      if (!artifacts.has(data.path)) {
        artifacts.add(data.path);
        addArtifactCard(artifactList, data);
      }

      // 在 SVG 就绪前，显示中间产物图片预览
      if (data.kind === "figure" || data.kind === "samed") {
        previewImage.src = data.url;
        previewImage.classList.add("visible");
      }

      if (data.kind === "template_svg" || data.kind === "optimized_svg" || data.kind === "final_svg") {
        // 隐藏预览层，显示 SVG 编辑器
        previewOverlay.classList.add("hidden");
        await loadSvgAsset(data.url);
      }

      if (stepMap[data.kind] && stepMap[data.kind].step > currentStep) {
        updateProgress(stepMap[data.kind].step);
        statusText.textContent = `第 ${currentStep}/5 步 - ${stepMap[data.kind].label}`;
      }
    });

    eventSource.addEventListener("status", (event) => {
      const data = JSON.parse(event.data);
      if (data.state === "started") {
        statusText.textContent = "运行中";
        cancelBtn.style.display = "";
      } else if (data.state === "finished") {
        isFinished = true;
        cancelBtn.style.display = "none";
        if (typeof data.code === "number" && data.code !== 0) {
          const errorDetail = data.error || "";
          statusText.innerHTML = `<span style="color:#e74c3c">生成失败</span>`;
          progressFill.style.background = "linear-gradient(90deg, #c44536, rgba(196, 69, 54, 0.6))";
          if (errorDetail) {
            const errorEl = document.createElement("div");
            errorEl.className = "canvas-error";
            errorEl.textContent = errorDetail;
            statusText.parentElement.appendChild(errorEl);
          }
          logPanel.classList.add("open");
        } else {
          statusText.textContent = "已完成";
          updateProgress(5);
          previewOverlay.classList.add("hidden");
        }
      }
    });

    eventSource.addEventListener("log", (event) => {
      const data = JSON.parse(event.data);
      appendLogLine(logBody, data);
    });

    eventSource.onerror = () => {
      if (isFinished) {
        eventSource.close();
        return;
      }
      statusText.textContent = "连接已断开";
      cancelBtn.style.display = "none";
    };

    async function loadSvgAsset(url) {
      let svgText = "";
      try {
        const response = await fetch(url);
        svgText = await response.text();
      } catch (err) {
        return;
      }

      if (svgEditAvailable) {
        if (!svgEditPath) {
          return;
        }
        if (!svgReady) {
          pendingSvgText = svgText;
          return;
        }

        const loaded = tryLoadSvg(svgText);
        if (!loaded) {
          iframe.src = `${svgEditPath}?url=${encodeURIComponent(url)}`;
        }
      } else {
        fallbackObject.data = url;
      }
    }

    function tryLoadSvg(svgText) {
      if (!iframe.contentWindow) {
        return false;
      }

      const win = iframe.contentWindow;
      if (win.svgEditor && typeof win.svgEditor.loadFromString === "function") {
        win.svgEditor.loadFromString(svgText);
        return true;
      }
      if (win.svgCanvas && typeof win.svgCanvas.setSvgString === "function") {
        win.svgCanvas.setSvgString(svgText);
        return true;
      }
      return false;
    }
  }

  function appendLogLine(container, data) {
    const line = `[${data.stream}] ${data.line}`;
    const lines = container.textContent.split("\n").filter(Boolean);
    lines.push(line);
    if (lines.length > 200) {
      lines.splice(0, lines.length - 200);
    }
    container.textContent = lines.join("\n");
    container.scrollTop = container.scrollHeight;
  }

  function addArtifactCard(container, data) {
    const card = document.createElement("a");
    card.className = "artifact-card";
    card.href = data.url;
    card.target = "_blank";
    card.rel = "noreferrer";

    const img = document.createElement("img");
    img.src = data.url;
    img.alt = data.name;
    img.loading = "lazy";

    const meta = document.createElement("div");
    meta.className = "artifact-meta";

    const name = document.createElement("div");
    name.className = "artifact-name";
    name.textContent = data.name;

    const badge = document.createElement("div");
    badge.className = "artifact-badge";
    badge.textContent = formatKind(data.kind);

    meta.appendChild(name);
    meta.appendChild(badge);
    card.appendChild(img);
    card.appendChild(meta);
    container.prepend(card);
  }

  function formatKind(kind) {
    switch (kind) {
      case "figure":
        return "生成图";
      case "samed":
        return "分割图";
      case "icon_raw":
        return "原始图标";
      case "icon_nobg":
        return "去背景图标";
      case "template_svg":
        return "模板";
      case "optimized_svg":
        return "优化模板";
      case "final_svg":
        return "最终版";
      default:
        return "产物";
    }
  }
})();
