(() => {
  const page = document.body.dataset.page;
  if (page === "input") initInputPage();
  else if (page === "canvas") initCanvasPage();

  function $(id) { return document.getElementById(id); }

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

  /* ========== Input Page ========== */
  function initInputPage() {
    const confirmBtn = $("confirmBtn");
    const errorMsg = $("errorMsg");
    const uploadZone = $("uploadZone");
    const referenceFile = $("referenceFile");
    const referencePreview = $("referencePreview");
    const referenceStatus = $("referenceStatus");
    const uploadPlaceholder = $("uploadPlaceholder");
    const uploadPreviewWrap = $("uploadPreviewWrap");
    const uploadRemove = $("uploadRemove");
    const methodText = $("methodText");
    const chineseHint = $("chineseHint");
    const inviteCodeInput = $("inviteCodeInput");
    const inviteStatus = $("inviteStatus");

    // 邀请码：从 localStorage 恢复
    const savedCode = localStorage.getItem("invite_code");
    if (savedCode && inviteCodeInput) {
      inviteCodeInput.value = savedCode;
      inviteStatus.textContent = "已保存的邀请码";
      inviteStatus.style.color = "#10b981";
    }

    // 中文检测
    if (methodText && chineseHint) {
      methodText.addEventListener("input", () => {
        const hasChinese = /[\u4e00-\u9fff]/.test(methodText.value);
        chineseHint.style.display = hasChinese ? "flex" : "none";
      });
    }

    // 上传区域
    if (uploadZone && referenceFile) {
      referenceFile.addEventListener("click", (e) => e.stopPropagation());
      uploadZone.addEventListener("click", () => referenceFile.click());
      uploadZone.addEventListener("dragover", (e) => { e.preventDefault(); uploadZone.classList.add("dragging"); });
      uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("dragging"));
      uploadZone.addEventListener("drop", (e) => {
        e.preventDefault();
        uploadZone.classList.remove("dragging");
        if (e.dataTransfer.files[0]) uploadReference(e.dataTransfer.files[0]);
      });
      referenceFile.addEventListener("change", () => {
        if (referenceFile.files[0]) uploadReference(referenceFile.files[0]);
      });
    }

    // 删除上传图片
    if (uploadRemove) {
      uploadRemove.addEventListener("click", (e) => {
        e.stopPropagation();
        $("referenceImage").value = "";
        referenceFile.value = "";
        uploadPreviewWrap.style.display = "none";
        uploadPlaceholder.style.display = "";
        referenceStatus.textContent = "";
      });
    }

    const progressArea = $("progressArea");
    const progressLabel = $("progressLabel");
    const progressTrackFill = $("progressTrackFill");

    const stepLabels = {
      figure:        { step: 1, text: "步骤 1/5：正在生成图片..." },
      samed:         { step: 2, text: "步骤 2/5：正在分析图片元素..." },
      icon_raw:      { step: 3, text: "步骤 3/5：正在处理图标..." },
      icon_nobg:     { step: 3, text: "步骤 3/5：正在处理图标..." },
      template_svg:  { step: 4, text: "步骤 4/5：正在生成 SVG 模板..." },
      optimized_svg: { step: 4, text: "步骤 4/5：正在优化 SVG 模板..." },
      final_svg:     { step: 5, text: "步骤 5/5：正在组装最终 SVG..." },
    };

    const stepTimes = {
      1: "预计还需 2-3 分钟",
      2: "预计还需 1-2 分钟",
      3: "预计还需 1 分钟",
      4: "预计还需 30 秒",
      5: "即将完成...",
    };

    confirmBtn.addEventListener("click", async () => {
      errorMsg.textContent = "";
      const text = methodText.value.trim();
      if (!text) {
        errorMsg.textContent = "请输入论文方法描述文本。";
        return;
      }

      // 检查邀请码
      const inviteCode = inviteCodeInput ? inviteCodeInput.value.trim() : "";
      if (!inviteCode) {
        errorMsg.textContent = "请输入邀请码";
        if (inviteCodeInput) inviteCodeInput.focus();
        return;
      }

      confirmBtn.disabled = true;
      confirmBtn.innerHTML = '<span class="btn-spinner"></span>生成中...';
      progressArea.style.display = "";
      progressLabel.textContent = "正在启动任务...";
      progressTrackFill.style.width = "0%";

      // 如果检测到中文，先显示翻译提示
      if (/[\u4e00-\u9fff]/.test(text)) {
        progressLabel.textContent = "正在翻译中文文本...";
        progressTrackFill.style.width = "2%";
      }

      const refPath = $("referenceImage") ? $("referenceImage").value.trim() : null;
      const payload = {
        method_text: text,
        optimize_iterations: parseInt($("optimizeIterations").value, 10),
        reference_image_path: refPath || null,
        invite_code: inviteCode,
      };

      let jobId = null;
      try {
        const response = await fetch("/api/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          const t = await response.text();
          if (response.status === 403) {
            // 邀请码问题，清除缓存
            localStorage.removeItem("invite_code");
            if (inviteStatus) { inviteStatus.textContent = parseErrorMessage(t); inviteStatus.style.color = "#dc3545"; }
            if (inviteCodeInput) inviteCodeInput.focus();
          }
          throw new Error(parseErrorMessage(t || "请求失败"));
        }
        const data = await response.json();
        jobId = data.job_id;
        // 成功后保存邀请码到 localStorage
        localStorage.setItem("invite_code", inviteCode);
        if (inviteStatus) { inviteStatus.textContent = "邀请码有效"; inviteStatus.style.color = "#10b981"; }
      } catch (err) {
        errorMsg.textContent = err.message || "启动任务失败";
        confirmBtn.disabled = false;
        confirmBtn.textContent = "开始生成";
        progressArea.style.display = "none";
        return;
      }

      progressLabel.textContent = "步骤 1/5：正在生成图片...";
      progressTrackFill.style.width = "5%";

      let currentStep = 0;
      let sseFinished = false;
      let retryCount = 0;
      const MAX_RETRIES = 5;

      function connectSSE() {
        const es = new EventSource(`/api/events/${jobId}`);

        es.addEventListener("artifact", (event) => {
          retryCount = 0;
          const data = JSON.parse(event.data);
          const info = stepLabels[data.kind];
          if (info && info.step > currentStep) {
            currentStep = info.step;
            const time = stepTimes[currentStep] || "";
            progressLabel.textContent = `${info.text} ${time}`;
            progressTrackFill.style.width = (currentStep / 5 * 100) + "%";
            confirmBtn.innerHTML = `<span class="btn-spinner"></span>生成中... ${time}`;
          }
        });

        es.addEventListener("status", (event) => {
          const data = JSON.parse(event.data);
          if (data.state === "finished") {
            sseFinished = true;
            es.close();
            if (typeof data.code === "number" && data.code !== 0) {
              errorMsg.textContent = data.error || "生成失败，请查看日志了解详情。";
              confirmBtn.disabled = false;
              confirmBtn.textContent = "开始生成";
              progressArea.style.display = "none";
            } else {
              progressLabel.textContent = "生成完成，正在跳转到结果页...";
              progressTrackFill.style.width = "100%";
              setTimeout(() => {
                window.location.href = `/canvas.html?job=${encodeURIComponent(jobId)}`;
              }, 600);
            }
          }
        });

        es.onerror = () => {
          es.close();
          if (sseFinished) return;
          retryCount++;
          if (retryCount <= MAX_RETRIES) {
            progressLabel.textContent = "连接中断，正在重连...";
            setTimeout(connectSSE, 3000);
          } else {
            // 重连多次失败，跳转到结果页查看
            window.location.href = `/canvas.html?job=${encodeURIComponent(jobId)}`;
          }
        };
      }

      connectSSE();
    });

    async function uploadReference(file) {
      if (!file.type.startsWith("image/")) {
        referenceStatus.textContent = "仅支持图片文件。";
        return;
      }
      confirmBtn.disabled = true;
      referenceStatus.textContent = "正在上传参考图...";

      const formData = new FormData();
      formData.append("file", file);

      try {
        const response = await fetch("/api/upload", { method: "POST", body: formData });
        if (!response.ok) {
          const text = await response.text();
          throw new Error(parseErrorMessage(text || "上传失败"));
        }
        const data = await response.json();
        $("referenceImage").value = data.path;
        referenceStatus.textContent = `已上传：${data.name}`;
        if (referencePreview) {
          referencePreview.src = data.url || "";
          uploadPlaceholder.style.display = "none";
          uploadPreviewWrap.style.display = "";
        }
      } catch (err) {
        referenceStatus.textContent = err.message || "上传失败";
      } finally {
        confirmBtn.disabled = false;
      }
    }
  }

  /* ========== Canvas / Results Page ========== */
  function initCanvasPage() {
    const params = new URLSearchParams(window.location.search);
    const jobId = params.get("job");
    const statusText = $("statusText");
    const statusChip = $("statusChip");
    const jobIdEl = $("jobId");
    const logToggle = $("logToggle");
    const logPanel = $("logPanel");
    const logClose = $("logClose");
    const logBody = $("logBody");
    const cancelBtn = $("cancelBtn");
    const progressFill = $("progressFill");
    const progressSteps = $("progressSteps");
    const progressSection = $("progressSection");
    const progressStatus = $("progressStatus");
    const figureCard = $("figureCard");
    const figureImg = $("figureImg");
    const figureDlBtn = $("figureDlBtn");
    const svgCard = $("svgCard");
    const svgList = $("svgList");
    const downloadAllSvg = $("downloadAllSvg");
    const errorCard = $("errorCard");
    const errorDetail = $("errorDetail");
    const resultFooter = $("resultFooter");

    if (!jobId) {
      statusText.textContent = "缺少任务 ID";
      return;
    }

    jobIdEl.textContent = jobId;

    // Log panel toggles
    logToggle.addEventListener("click", () => {
      logPanel.classList.toggle("open");
      if (logPanel.classList.contains("open")) fetchLogs();
    });
    if (logClose) logClose.addEventListener("click", () => logPanel.classList.remove("open"));

    // Cancel
    cancelBtn.addEventListener("click", async () => {
      if (!confirm("确定要取消当前任务吗？")) return;
      try {
        await fetch(`/api/cancel/${jobId}`, { method: "POST" });
        statusText.textContent = "已取消";
        cancelBtn.style.display = "none";
      } catch (_) {}
    });

    const stepMap = {
      figure:        { step: 1, label: "图片已生成" },
      samed:         { step: 2, label: "SAM 分割完成" },
      icon_raw:      { step: 3, label: "图标已提取" },
      icon_nobg:     { step: 3, label: "图标已去背景" },
      template_svg:  { step: 4, label: "模板 SVG 已就绪" },
      optimized_svg: { step: 4, label: "优化模板已就绪" },
      final_svg:     { step: 5, label: "最终 SVG 已就绪" },
    };

    const statusLabels = {
      0: "正在生成图片...",
      1: "正在进行 SAM 分割...",
      2: "正在提取图标...",
      3: "正在生成 SVG 模板...",
      4: "正在最终合成...",
    };

    let currentStep = 0;
    const collectedSvgs = [];

    function updateProgress(step) {
      currentStep = step;
      const pct = Math.round((step / 5) * 100);
      progressFill.style.width = pct + "%";

      const items = progressSteps.querySelectorAll(".step-item");
      items.forEach((el) => {
        const s = parseInt(el.dataset.step, 10);
        el.classList.toggle("done", s < step);
        el.classList.toggle("active", s === step);
      });

      if (statusLabels[step] !== undefined) {
        progressStatus.textContent = statusLabels[step];
      }
    }

    const artifacts = new Set();
    let isFinished = false;
    let sseRetryCount = 0;
    const SSE_MAX_RETRIES = 8;

    function connectSSE() {
      const es = new EventSource(`/api/events/${jobId}`);

      es.addEventListener("artifact", (event) => {
        sseRetryCount = 0;
        const data = JSON.parse(event.data);
        if (artifacts.has(data.path)) return;
        artifacts.add(data.path);

        if (data.kind === "figure") {
          figureCard.style.display = "";
          figureImg.src = data.url;
          figureDlBtn.href = data.url;
          figureDlBtn.download = data.name;
        }

        if (data.kind === "template_svg" || data.kind === "optimized_svg" || data.kind === "final_svg") {
          collectedSvgs.push(data);
          renderSvgList();
        }

        if (stepMap[data.kind] && stepMap[data.kind].step > currentStep) {
          updateProgress(stepMap[data.kind].step);
          statusText.textContent = `${currentStep}/5 - ${stepMap[data.kind].label}`;
        }
      });

      es.addEventListener("status", (event) => {
        sseRetryCount = 0;
        const data = JSON.parse(event.data);
        if (data.state === "started") {
          statusText.textContent = "运行中";
          cancelBtn.style.display = "";
        } else if (data.state === "finished") {
          isFinished = true;
          es.close();
          cancelBtn.style.display = "none";
          stopLogPolling();
          fetchLogs(); // 最后拉取一次完整日志
          if (typeof data.code === "number" && data.code !== 0) {
            statusText.textContent = "生成失败";
            statusChip.classList.add("error");
            errorCard.style.display = "";
            errorDetail.textContent = data.error || "未知错误，请查看日志。";
            logPanel.classList.add("open");
            progressSection.style.display = "none";
          } else {
            statusText.textContent = "已完成";
            statusChip.classList.add("done");
            updateProgress(5);
            progressStatus.textContent = "所有步骤已完成";
            resultFooter.style.display = "";
            fetchArtifactList();
          }
        }
      });

      es.addEventListener("log", (event) => {
        sseRetryCount = 0;
        // 日志通过 HTTP 轮询获取，SSE log 事件仅用于重置重试计数
      });

      es.onerror = () => {
        es.close();
        if (isFinished) return;
        sseRetryCount++;
        if (sseRetryCount <= SSE_MAX_RETRIES) {
          statusText.textContent = "连接中断，正在重连...";
          setTimeout(connectSSE, 3000);
        } else {
          statusText.textContent = "连接已断开";
          cancelBtn.style.display = "none";
          stopLogPolling();
          fetchLogs();
          fetchArtifactList();
        }
      };
    }

    connectSSE();

    function renderSvgList() {
      svgCard.style.display = "";
      svgList.innerHTML = "";

      const kindLabels = {
        template_svg: "模板 SVG",
        optimized_svg: "优化模板",
        final_svg: "最终版本",
      };

      collectedSvgs.forEach((svg) => {
        const item = document.createElement("div");
        item.className = "svg-item";

        const preview = document.createElement("div");
        preview.className = "svg-item-preview";
        const obj = document.createElement("object");
        obj.type = "image/svg+xml";
        obj.data = svg.url;
        preview.appendChild(obj);

        const info = document.createElement("div");
        info.className = "svg-item-info";
        info.innerHTML = `<div class="svg-item-name">${svg.name}</div><div class="svg-item-kind">${kindLabels[svg.kind] || svg.kind}</div>`;

        const dl = document.createElement("a");
        dl.className = "btn-download";
        dl.href = svg.url;
        dl.download = svg.name;
        dl.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 2v8m0 0l-3-3m3 3l3-3M3 12h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>下载';

        item.appendChild(preview);
        item.appendChild(info);
        item.appendChild(dl);
        svgList.appendChild(item);
      });
    }

    // Download all SVGs
    if (downloadAllSvg) {
      downloadAllSvg.addEventListener("click", () => {
        collectedSvgs.forEach((svg) => {
          const a = document.createElement("a");
          a.href = svg.url;
          a.download = svg.name;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
        });
      });
    }

    // 通过 HTTP 获取日志内容（不依赖 SSE）
    let logPollTimer = null;
    async function fetchLogs() {
      try {
        const res = await fetch(`/api/logs/${jobId}`);
        if (!res.ok) return;
        const text = await res.text();
        if (text) {
          logBody.textContent = text;
          logBody.scrollTop = logBody.scrollHeight;
        }
      } catch (_) {}
    }

    // 定期拉取日志（每 3 秒）
    function startLogPolling() {
      if (logPollTimer) return;
      logPollTimer = setInterval(() => {
        fetchLogs();
      }, 3000);
    }

    function stopLogPolling() {
      if (logPollTimer) {
        clearInterval(logPollTimer);
        logPollTimer = null;
      }
    }

    // 页面加载后开始轮询日志
    startLogPolling();

    async function fetchArtifactList() {
      try {
        const res = await fetch(`/api/artifacts-list/${jobId}`);
        if (!res.ok) return;
        const items = await res.json();

        items.forEach((item) => {
          if (artifacts.has(item.path)) return;
          artifacts.add(item.path);

          if (item.kind === "figure" && figureCard.style.display === "none") {
            figureCard.style.display = "";
            figureImg.src = item.url;
            figureDlBtn.href = item.url;
            figureDlBtn.download = item.name;
          }

          if (item.kind === "template_svg" || item.kind === "optimized_svg" || item.kind === "final_svg") {
            if (!collectedSvgs.find(s => s.path === item.path)) {
              collectedSvgs.push(item);
            }
          }
        });

        if (collectedSvgs.length > 0) renderSvgList();
        if (figureCard.style.display !== "none" || collectedSvgs.length > 0) {
          resultFooter.style.display = "";
        }
      } catch (_) {}
    }
  }

  /* ========== Shared Helpers ========== */
  function appendLogLine(container, data) {
    const line = `[${data.stream}] ${data.line}`;
    const lines = container.textContent.split("\n").filter(Boolean);
    lines.push(line);
    if (lines.length > 200) lines.splice(0, lines.length - 200);
    container.textContent = lines.join("\n");
    container.scrollTop = container.scrollHeight;
  }
})();
