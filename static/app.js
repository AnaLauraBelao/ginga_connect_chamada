(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  const startButton = $("#start-scanner");
  if (startButton) {
    let stream = null;
    let running = false;
    const reader = $("#reader");
    const status = $("#scan-status");

    const setStatus = (message, isError = false) => {
      status.textContent = message;
      status.classList.toggle("text-danger", isError);
    };

    const submitCredential = async (value) => {
      if (!value) return;
      setStatus("Validando crachá…");
      try {
        const response = await fetch(window.CAPOEIRA.scanUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": window.CAPOEIRA.csrf,
          },
          body: JSON.stringify({ qr_value: value }),
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || "Não foi possível validar o crachá.");
        setStatus(`Acesso liberado. Olá, ${result.name}!`);
        stopCamera();
        window.location.assign(result.redirect);
      } catch (error) {
        setStatus(error.message || "Falha ao validar o crachá.", true);
      }
    };

    const stopCamera = () => {
      running = false;
      if (stream) stream.getTracks().forEach(track => track.stop());
      stream = null;
    };

    startButton.addEventListener("click", async () => {
      if (!("BarcodeDetector" in window)) {
        setStatus("Este navegador não oferece leitura de QR nativa. Use Chrome/Edge atualizado ou insira o código manualmente.", true);
        return;
      }
      try {
        const detector = new BarcodeDetector({ formats: ["qr_code"] });
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
        reader.innerHTML = "";
        const video = document.createElement("video");
        video.setAttribute("playsinline", "");
        video.muted = true;
        reader.appendChild(video);
        reader.classList.remove("hidden");
        video.srcObject = stream;
        await video.play();
        running = true;
        startButton.textContent = "Câmera ativa — aponte para o QR";
        startButton.disabled = true;
        setStatus("Procurando QR Code…");
        const scanFrame = async () => {
          if (!running) return;
          try {
            const codes = await detector.detect(video);
            if (codes.length && codes[0].rawValue) {
              stopCamera();
              await submitCredential(codes[0].rawValue);
              startButton.disabled = false;
              startButton.textContent = "Escanear novamente";
              return;
            }
          } catch (_error) {
            // A próxima captura tenta novamente; erros transitórios podem ocorrer durante o foco.
          }
          if (running) requestAnimationFrame(scanFrame);
        };
        requestAnimationFrame(scanFrame);
      } catch (error) {
        stopCamera();
        startButton.disabled = false;
        startButton.textContent = "Escanear crachá";
        setStatus("Não foi possível abrir a câmera. Verifique a permissão e use HTTPS ou localhost.", true);
      }
    });

    const manualForm = $("#manual-scan-form");
    if (manualForm) manualForm.addEventListener("submit", (event) => {
      event.preventDefault();
      submitCredential($("#qr-value").value.trim());
    });
    window.addEventListener("pagehide", stopCamera);
  }

  const selectAll = $("#select-all");
  const attendanceChecks = () => $$('#attendance-roster input[name="student_ids"]');
  const updateAttendanceCount = () => {
    const checks = attendanceChecks();
    const selected = checks.filter(item => item.checked).length;
    const label = $("#selected-count");
    if (label) label.textContent = `${selected} selecionado(s)`;
    if (selectAll) {
      selectAll.checked = checks.length > 0 && selected === checks.length;
      selectAll.indeterminate = selected > 0 && selected < checks.length;
    }
  };
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      attendanceChecks().forEach(item => { item.checked = selectAll.checked; });
      updateAttendanceCount();
    });
    document.addEventListener("change", event => {
      if (event.target.matches('#attendance-roster input[name="student_ids"]')) updateAttendanceCount();
    });
    updateAttendanceCount();
  }
  window.updateAttendanceCount = updateAttendanceCount;

  // Student badge scanner and quick-create dialog, both available directly on roll call.
  const attendanceConfig = window.CAPOEIRA_ATTENDANCE;
  const scanStart = $("#student-scan-start");
  if (attendanceConfig && scanStart) {
    const reader = $("#student-reader");
    const scanStatus = $("#student-scan-status");
    const qrInput = $("#student-qr-value");
    let cameraStream = null;
    let cameraRunning = false;
    let scanBusy = false;

    const setScanStatus = (message, error = false) => {
      scanStatus.textContent = message;
      scanStatus.classList.toggle("text-danger", error);
    };
    const stopStudentCamera = () => {
      cameraRunning = false;
      if (cameraStream) cameraStream.getTracks().forEach(track => track.stop());
      cameraStream = null;
      reader.classList.add("hidden");
      reader.innerHTML = "";
      scanStart.disabled = false;
      scanStart.textContent = "Escanear crachá";
    };
    const addStudentToAttendance = student => {
      const roster = $("#attendance-roster");
      if (!roster) return;
      let label = roster.querySelector(`[data-student-id="${student.id}"]`);
      let checkbox;
      if (!label) {
        const emptyState = $("#attendance-empty");
        if (emptyState) emptyState.remove();
        label = document.createElement("label");
        label.className = "attendance-person attendance-person-new";
        label.dataset.studentId = student.id;
        checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.name = "student_ids";
        checkbox.value = student.id;
        const avatar = document.createElement("span");
        avatar.className = "avatar";
        avatar.textContent = (student.nickname || student.name || "?").charAt(0).toUpperCase();
        const main = document.createElement("span");
        main.className = "person-main";
        const display = document.createElement("strong");
        display.textContent = student.nickname || student.name;
        const details = document.createElement("small");
        details.textContent = `${student.name} · ${student.belt || "Corda não informada"}`;
        main.append(display, details);
        const check = document.createElement("span");
        check.className = "attendance-check";
        check.textContent = "✓";
        label.append(checkbox, avatar, main, check);
        roster.appendChild(label);
      } else {
        checkbox = label.querySelector('input[name="student_ids"]');
      }
      checkbox.checked = true;
      label.classList.add("attendance-person-new");
      label.scrollIntoView({ behavior: "smooth", block: "nearest" });
      const total = attendanceChecks().length;
      const rosterCount = $("#roster-count");
      const rosterCountMeta = $("#roster-count-meta");
      if (rosterCount) rosterCount.textContent = `${total} aluno(s) disponível(is)`;
      if (rosterCountMeta) rosterCountMeta.textContent = `${total} aluno(s) na lista`;
      updateAttendanceCount();
    };
    const validateStudentQr = async value => {
      const qrValue = (value || "").trim();
      if (!qrValue || scanBusy) return;
      scanBusy = true;
      setScanStatus("Validando crachá do aluno…");
      try {
        const response = await fetch(attendanceConfig.scanUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": attendanceConfig.csrf },
          body: JSON.stringify({ qr_value: qrValue })
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || "Não foi possível validar o crachá.");
        addStudentToAttendance(result.student);
        setScanStatus(`Presença selecionada: ${result.student.nickname || result.student.name}. Pode escanear o próximo crachá.`);
        if (qrInput) qrInput.value = "";
      } catch (error) {
        setScanStatus(error.message || "Falha ao validar o crachá.", true);
      } finally {
        scanBusy = false;
      }
    };

    scanStart.addEventListener("click", async () => {
      if (cameraRunning) {
        stopStudentCamera();
        setScanStatus("Leitor desligado.");
        return;
      }
      if (!("BarcodeDetector" in window)) {
        setScanStatus("Este navegador não oferece leitura de QR nativa. Use Chrome/Edge atualizado ou informe o código manualmente abaixo.", true);
        return;
      }
      if (!navigator.mediaDevices?.getUserMedia) {
        setScanStatus("A câmera não está disponível. Acesse por HTTPS ou localhost, ou informe o código manualmente.", true);
        return;
      }
      try {
        const detector = new BarcodeDetector({ formats: ["qr_code"] });
        cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
        reader.innerHTML = "";
        const video = document.createElement("video");
        video.setAttribute("playsinline", "");
        video.muted = true;
        reader.appendChild(video);
        reader.classList.remove("hidden");
        video.srcObject = cameraStream;
        await video.play();
        cameraRunning = true;
        scanStart.textContent = "Desligar leitor";
        setScanStatus("Câmera ativa. Aponte para o QR Code dos crachás.");
        const scanFrame = async () => {
          if (!cameraRunning) return;
          try {
            const codes = await detector.detect(video);
            if (codes.length && codes[0].rawValue && !scanBusy) {
              await validateStudentQr(codes[0].rawValue);
            }
          } catch (_error) {
            // Keep scanning; autofocus can trigger transient detector errors.
          }
          if (cameraRunning) requestAnimationFrame(scanFrame);
        };
        requestAnimationFrame(scanFrame);
      } catch (_error) {
        stopStudentCamera();
        setScanStatus("Não foi possível abrir a câmera. Verifique a permissão e use HTTPS ou localhost.", true);
      }
    });

    $("#student-manual-scan")?.addEventListener("click", () => validateStudentQr(qrInput.value));
    qrInput?.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        validateStudentQr(qrInput.value);
      }
    });
    window.addEventListener("pagehide", stopStudentCamera);

    const modal = $("#quick-student-modal");
    const openModal = $("#open-quick-student");
    const closeModal = () => {
      modal.classList.add("hidden");
      $("#quick-student-status").textContent = "";
      const saveButton = $("#save-quick-student");
      saveButton.disabled = false;
      saveButton.dataset.created = "false";
      saveButton.textContent = "Cadastrar e adicionar";
      openModal?.focus();
    };
    openModal?.addEventListener("click", () => {
      modal.classList.remove("hidden");
      $("#quick-student-name")?.focus();
    });
    $("#close-quick-student")?.addEventListener("click", closeModal);
    $("#cancel-quick-student")?.addEventListener("click", closeModal);
    modal?.addEventListener("click", event => { if (event.target === modal) closeModal(); });
    document.addEventListener("keydown", event => { if (event.key === "Escape" && modal && !modal.classList.contains("hidden")) closeModal(); });

    $("#save-quick-student")?.addEventListener("click", async () => {
      const saveButton = $("#save-quick-student");
      if (saveButton.dataset.created === "true") {
        saveButton.dataset.created = "false";
        closeModal();
        return;
      }
      const status = $("#quick-student-status");
      const name = $("#quick-student-name").value.trim();
      const nickname = $("#quick-student-nickname").value.trim();
      const belt = $("#quick-student-belt").value.trim();
      if (!name) {
        status.textContent = "Informe o nome completo do aluno.";
        status.classList.add("text-danger");
        $("#quick-student-name").focus();
        return;
      }
      const regularClassIds = $$('input[name="quick-regular-class"]:checked').map(input => Number(input.value));
      saveButton.disabled = true;
      status.classList.remove("text-danger");
      status.textContent = "Cadastrando aluno…";
      try {
        const response = await fetch(attendanceConfig.quickStudentUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": attendanceConfig.csrf },
          body: JSON.stringify({ name, nickname, belt, regular_class_ids: regularClassIds })
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || "Não foi possível cadastrar o aluno.");
        addStudentToAttendance(result.student);
        status.classList.remove("text-danger");
        status.textContent = "Aluno cadastrado e selecionado na chamada.";
        const badgeLink = document.createElement("a");
        badgeLink.href = result.badge_url;
        badgeLink.className = "text-link quick-badge-link";
        badgeLink.textContent = "Baixar crachá PDF";
        status.appendChild(document.createTextNode(" "));
        status.appendChild(badgeLink);
        $("#quick-student-name").value = "";
        $("#quick-student-nickname").value = "";
        $("#quick-student-belt").value = "";
        $$('#quick-student-modal input[name="quick-regular-class"]').forEach(input => { input.checked = false; });
        saveButton.dataset.created = "true";
        saveButton.disabled = false;
        saveButton.textContent = "Fechar";
      } catch (error) {
        status.classList.add("text-danger");
        status.textContent = error.message || "Falha ao cadastrar aluno.";
        saveButton.disabled = false;
      }
    });
  }

  // Copy the preformatted frequency report, with a manual-selection fallback.
  const reportText = $("#report-text");
  const copyStatus = $("#copy-report-status");
  const copyReport = async () => {
    if (!reportText) return;
    const fallbackCopy = () => {
      reportText.focus();
      reportText.select();
      return document.execCommand("copy");
    };
    try {
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(reportText.value);
        } catch (_clipboardError) {
          if (!fallbackCopy()) throw _clipboardError;
        }
      } else if (!fallbackCopy()) {
        throw new Error("O navegador não permitiu copiar automaticamente.");
      }
      if (copyStatus) {
        copyStatus.textContent = "Relatório copiado para a área de transferência!";
        copyStatus.classList.remove("text-danger");
      }
    } catch (_error) {
      reportText.focus();
      reportText.select();
      if (copyStatus) {
        copyStatus.textContent = "Não foi possível copiar automaticamente. O texto está selecionado: use Ctrl+C (ou toque em Copiar).";
        copyStatus.classList.add("text-danger");
      }
    }
  };
  $("#copy-report-button")?.addEventListener("click", copyReport);
  $("#copy-report-button-secondary")?.addEventListener("click", copyReport);

  // Custom dates are required only when the combined report uses a custom range.
  const combinedPeriodSelect = $("#combined-period-select");
  const combinedDateInputs = $$('[data-combined-date]');
  const updateCombinedDateRequirements = () => {
    const isCustom = combinedPeriodSelect?.value === "custom";
    combinedDateInputs.forEach(input => { input.required = Boolean(isCustom); });
  };
  combinedPeriodSelect?.addEventListener("change", updateCombinedDateRequirements);
  updateCombinedDateRequirements();

  const typeRadios = $$('[data-class-type]');
  const extraLinks = $("#extra-links");
  if (typeRadios.length && extraLinks) {
    const toggle = () => {
      const selected = typeRadios.find(item => item.checked);
      extraLinks.classList.toggle("hidden", !selected || selected.value !== "extra");
    };
    typeRadios.forEach(item => item.addEventListener("change", toggle));
    toggle();
  }
})();
