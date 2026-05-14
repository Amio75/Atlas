const chatMessages = document.getElementById("chat-messages");
const chatForm = document.getElementById("chat-form");
const messageInput = document.getElementById("message-input");
const statusLabel = document.getElementById("status");
const attachmentTrigger = document.getElementById("attachment-trigger");
const attachmentMenu = document.getElementById("attachment-menu");
const attachmentStatus = document.getElementById("attachment-status");
const prescriptionUpload = document.getElementById("prescription-upload");
const pendingAttachmentPreview = document.getElementById("pending-attachment-preview");
const pendingAttachmentImage = document.getElementById("pending-attachment-image");
const pendingAttachmentName = document.getElementById("pending-attachment-name");
const pendingAttachmentRemove = document.getElementById("pending-attachment-remove");
const voiceInputTrigger = document.getElementById("voice-input-trigger");
const voiceOutputToggle = document.getElementById("voice-output-toggle");
const voiceStatus = document.getElementById("voice-status");
const connectionPill = document.getElementById("connection-pill");
const liveAgentStatus = document.getElementById("live-agent-status");
const liveAgentPhase = document.getElementById("live-agent-phase");
const liveAgentMessage = document.getElementById("live-agent-message");
const liveAgentSteps = document.getElementById("live-agent-steps");
const sendButton = document.getElementById("send-button");
  const sidebarToggle = document.getElementById("sidebar-toggle");
  const sidebarBackdrop = document.getElementById("sidebar-backdrop");

if (chatMessages && chatForm && messageInput && statusLabel) {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/chat`);
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const speechSynthesisSupported = "speechSynthesis" in window;
  const recognition = SpeechRecognition ? new SpeechRecognition() : null;
  let pendingAttachment = null;
  let isSendingAttachment = false;
  let voiceOutputEnabled = false;
  let voiceInputActive = false;
  let finalTranscript = "";
  let silenceTimer = null;
  let autoLoopVoice = false;
  let restartMicAfterSpeech = false;
  let activeAssistantStream = null;

  let activeRunInProgress = false;

  const escapeHtml = (value) =>
    String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");

  const scrollMessagesToBottom = () => {
    chatMessages.scrollTo({
      top: chatMessages.scrollHeight,
      behavior: "smooth"
    });
  };

  const autoResize = () => {
    messageInput.style.height = "auto";
    messageInput.style.height = `${messageInput.scrollHeight}px`;
  };

  const setVoiceStatus = (message) => {
    if (voiceStatus) {
      voiceStatus.textContent = message;
    }
  };

  const clearSilenceTimer = () => {
    if (silenceTimer) {
      window.clearTimeout(silenceTimer);
      silenceTimer = null;
    }
  };

  // Sidebar toggle for mobile
  const closeSidebar = () => {
    document.documentElement.classList.remove("has-sidebar-open");
    if (sidebarToggle) sidebarToggle.setAttribute("aria-expanded", "false");
  };

  const openSidebar = () => {
    document.documentElement.classList.add("has-sidebar-open");
    if (sidebarToggle) sidebarToggle.setAttribute("aria-expanded", "true");
  };

  if (sidebarToggle) {
    sidebarToggle.addEventListener("click", (e) => {
      const isOpen = document.documentElement.classList.toggle("has-sidebar-open");
      sidebarToggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
    });
  }

  if (sidebarBackdrop) {
    sidebarBackdrop.addEventListener("click", () => closeSidebar());
  }

  // Close on Escape key
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      closeSidebar();
    }
  });

  const scheduleAutoSendFromSilence = () => {
    clearSilenceTimer();
    silenceTimer = window.setTimeout(() => {
      if (!voiceInputActive) {
        return;
      }
      const hasMessage = messageInput.value.trim().length > 0;
      const hasAttachment = Boolean(pendingAttachment);
      recognition?.stop();
      if (hasMessage || hasAttachment) {
        setVoiceStatus("No speech detected for 3 seconds. Sending automatically.");
        chatForm.requestSubmit();
      }
    }, 3000);
  };

  const setVoiceOutputEnabled = (enabled) => {
    if (!speechSynthesisSupported || !voiceOutputToggle) {
      return;
    }

    voiceOutputEnabled = enabled;
    voiceOutputToggle.setAttribute("aria-pressed", enabled ? "true" : "false");
    voiceOutputToggle.classList.toggle("voice-active", enabled);

    if (!enabled) {
      restartMicAfterSpeech = false;
      window.speechSynthesis.cancel();
    }
  };

  const speakText = (text) => {
    if (!voiceOutputEnabled || !speechSynthesisSupported || !text) {
      return;
    }

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1;
    utterance.pitch = 1;
    utterance.lang = "en-US";
    utterance.onend = () => {
      if (autoLoopVoice && restartMicAfterSpeech && recognition) {
        restartMicAfterSpeech = false;
        finalTranscript = "";
        messageInput.value = "";
        autoResize();
        recognition.start();
      }
    };
    window.speechSynthesis.speak(utterance);
  };

  const buildTextContent = ({ sender, message, timestamp, variant, streaming = false }) => {
    const isUser = variant === "user";
    return `
      <article class="w-full max-w-[92%] rounded-[2rem] border px-4 py-4 shadow-lg sm:max-w-2xl sm:px-5 ${
        isUser
          ? "border-cyan-300/20 bg-gradient-to-br from-cyan-500/15 to-blue-500/15 text-slate-100"
          : "border-white/10 bg-white/5 text-slate-100"
      }">
        <div class="flex items-center justify-between gap-4">
          <p class="text-sm font-medium ${isUser ? "text-cyan-100" : "text-slate-300"}">${escapeHtml(sender)}</p>
          <p class="text-xs text-slate-500">${escapeHtml(timestamp)}</p>
        </div>
        <p class="mt-3 whitespace-pre-wrap text-sm leading-7 text-slate-100">${escapeHtml(message)}</p>
        ${
          streaming
            ? '<div class="mt-3 inline-flex items-center gap-2 rounded-full border border-cyan-300/20 bg-cyan-400/10 px-3 py-1 text-[11px] text-cyan-100">Streaming response...</div>'
            : ""
        }
      </article>
    `;
  };

  const buildImageContent = ({ sender, timestamp, imageUrl, serialNumber, caption, variant, pending }) => {
    const isUser = variant === "user";
    const pendingBadge = pending
      ? '<span class="rounded-full border border-amber-300/30 bg-amber-400/10 px-2 py-1 text-[11px] text-amber-200">Uploading...</span>'
      : "";
    const serialText = serialNumber ? `Image #${escapeHtml(String(serialNumber))}` : "Local preview";
    return `
      <article class="w-full max-w-[92%] rounded-[2rem] border px-3 py-3 shadow-lg sm:max-w-2xl sm:px-4 ${
        isUser
          ? "border-cyan-300/20 bg-gradient-to-br from-cyan-500/15 to-blue-500/15 text-slate-100"
          : "border-white/10 bg-white/5 text-slate-100"
      }">
        <div class="flex items-center justify-between gap-4 px-1 pb-3">
          <p class="text-sm font-medium ${isUser ? "text-cyan-100" : "text-slate-300"}">${escapeHtml(sender)}</p>
          <div class="flex items-center gap-2">
            ${pendingBadge}
            <p class="text-xs text-slate-500">${escapeHtml(timestamp)}</p>
          </div>
        </div>
        <img src="${escapeHtml(imageUrl)}" alt="Uploaded prescription" class="chat-upload-image w-full rounded-[1.5rem] object-cover" />
        <div class="mt-3 flex items-center justify-between gap-3 px-1">
          <p class="text-xs text-slate-300">${escapeHtml(caption || "Uploaded image")}</p>
          <p class="text-xs text-cyan-200">${serialText}</p>
        </div>
      </article>
    `;
  };

  const renderMessage = ({ sender, message, timestamp, variant, streaming = false }) => {
    const wrapper = document.createElement("div");
    wrapper.className = `message-card flex ${variant === "user" ? "justify-end" : "justify-start"}`;
    wrapper.innerHTML = buildTextContent({ sender, message, timestamp, variant, streaming });
    chatMessages.appendChild(wrapper);
    scrollMessagesToBottom();
    return wrapper;
  };

  const updateMessage = (wrapper, { sender, message, timestamp, variant, streaming = false }) => {
    wrapper.innerHTML = buildTextContent({ sender, message, timestamp, variant, streaming });
    scrollMessagesToBottom();
  };

  const renderImageMessage = ({ sender, timestamp, imageUrl, serialNumber, caption, variant, pending }) => {
    const wrapper = document.createElement("div");
    wrapper.className = `message-card flex ${variant === "user" ? "justify-end" : "justify-start"}`;
    wrapper.innerHTML = buildImageContent({
      sender,
      timestamp,
      imageUrl,
      serialNumber,
      caption,
      variant,
      pending
    });
    chatMessages.appendChild(wrapper);
    scrollMessagesToBottom();
    return wrapper;
  };

  const updateImageMessage = (wrapper, { sender, timestamp, imageUrl, serialNumber, caption, variant }) => {
    wrapper.innerHTML = buildImageContent({
      sender,
      timestamp,
      imageUrl,
      serialNumber,
      caption,
      variant,
      pending: false
    });
    scrollMessagesToBottom();
  };

  const setConnectionState = (connected) => {
    if (!connectionPill) {
      return;
    }

    connectionPill.textContent = connected ? "WebSocket connected" : "Connection closed";
    connectionPill.className = connected
      ? "rounded-full border border-emerald-400/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200 sm:px-4 sm:text-sm"
      : "rounded-full border border-rose-400/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200 sm:px-4 sm:text-sm";
  };

  const clearPendingAttachment = () => {
    if (pendingAttachment?.previewUrl) {
      URL.revokeObjectURL(pendingAttachment.previewUrl);
    }
    pendingAttachment = null;
    if (pendingAttachmentPreview) {
      pendingAttachmentPreview.classList.add("hidden");
    }
    if (pendingAttachmentImage) {
      pendingAttachmentImage.src = "";
    }
    if (pendingAttachmentName) {
      pendingAttachmentName.textContent = "";
    }
    if (prescriptionUpload) {
      prescriptionUpload.value = "";
    }
  };

  const setPendingAttachment = (file) => {
    clearPendingAttachment();
    const previewUrl = URL.createObjectURL(file);
    pendingAttachment = { file, previewUrl };

    if (pendingAttachmentPreview && pendingAttachmentImage && pendingAttachmentName) {
      pendingAttachmentImage.src = previewUrl;
      pendingAttachmentName.textContent = file.name;
      pendingAttachmentPreview.classList.remove("hidden");
    }

    if (attachmentStatus) {
      attachmentStatus.textContent = "Image is ready to upload. It will only be analyzed when you ask about it in chat.";
    }
  };

  const setAttachmentMenuOpen = (isOpen) => {
    if (!attachmentMenu || !attachmentTrigger) {
      return;
    }

    attachmentMenu.classList.toggle("pointer-events-none", !isOpen);
    attachmentMenu.classList.toggle("opacity-0", !isOpen);
    attachmentMenu.classList.toggle("translate-y-2", !isOpen);
    attachmentTrigger.setAttribute("aria-expanded", isOpen ? "true" : "false");
  };

  const setComposerBusyState = (busy) => {
    activeRunInProgress = busy;

    if (chatForm) {
      chatForm.classList.toggle("hidden", busy);
    }

    [messageInput, sendButton, attachmentTrigger, voiceInputTrigger, voiceOutputToggle].forEach((element) => {
      if (!element) {
        return;
      }

      if (busy) {
        element.setAttribute("disabled", "disabled");
        element.classList.add("opacity-60", "cursor-not-allowed");
      } else {
        element.removeAttribute("disabled");
        element.classList.remove("opacity-60", "cursor-not-allowed");
      }
    });
  };

  const updateLiveAgentStatus = ({ phase, message, done = false }) => {
    if (!liveAgentStatus || !liveAgentPhase || !liveAgentMessage || !liveAgentSteps) {
      return;
    }

    setComposerBusyState(!done);
    liveAgentStatus.classList.remove("hidden");
    liveAgentPhase.textContent = phase ? phase.replaceAll("_", " ") : "working";
    liveAgentMessage.textContent = message;
    liveAgentSteps.innerHTML = "";
  };

  const hideLiveAgentStatus = () => {
    if (!liveAgentStatus || activeRunInProgress) {
      return;
    }
    window.setTimeout(() => {
      if (!activeRunInProgress) {
        liveAgentStatus.classList.add("hidden");
        setComposerBusyState(false);
      }
    }, 1200);
  };

  const resetLiveAgentStatus = () => {
    if (!liveAgentStatus || !liveAgentSteps || !liveAgentPhase || !liveAgentMessage) {
      return;
    }

    setComposerBusyState(true);
    liveAgentStatus.classList.remove("hidden");
    liveAgentPhase.textContent = "starting";
    liveAgentMessage.textContent = "Preparing your request.";
    liveAgentSteps.innerHTML = "";
  };

  const buildImageQueryHint = (serialNumber) =>
    `Image #${serialNumber} uploaded. Ask in chat: "analyze image #${serialNumber}"`;

  const registerUploadedImage = (payload) => {
    if (attachmentStatus) {
      attachmentStatus.textContent =
        payload.message || buildImageQueryHint(payload.serial_number);
    }
  };

  const buildLoadingNotice = ({ phase, message, done, timestamp }) => {
    const phaseLabel = phase ? phase.replaceAll("_", " ") : "status";
    return `
      <article class="rounded-2xl border border-white/10 bg-slate-900/70 p-3">
        <div class="flex items-center justify-between gap-3">
          <p class="text-xs uppercase tracking-[0.2em] text-slate-400">${escapeHtml(phaseLabel)}</p>
          <p class="text-[11px] ${done ? "text-emerald-200" : "text-cyan-200"}">${done ? "Done" : "Running"}</p>
        </div>
        <p class="mt-2 text-sm leading-6 text-slate-200">${escapeHtml(message)}</p>
        <p class="mt-2 text-[11px] text-slate-500">${escapeHtml(timestamp || "")}</p>
      </article>
    `;
  };

  const uploadPendingAttachment = async () => {
    if (!pendingAttachment) {
      return null;
    }

    const { file, previewUrl } = pendingAttachment;
    const timestamp = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const previewMessage = renderImageMessage({
      sender: window.MEDIGEM_USER.name,
      timestamp,
      imageUrl: previewUrl,
      serialNumber: null,
      caption: file.name,
      variant: "user",
      pending: true
    });

    if (attachmentStatus) {
      attachmentStatus.textContent = "Uploading image now. Vision analysis will wait until you ask for it.";
    }

    const formData = new FormData();
    formData.append("file", file);

    const response = await fetch("/api/uploads/prescription", {
      method: "POST",
      body: formData
    });
    const payload = await response.json();

    if (!response.ok) {
      previewMessage.remove();
      throw new Error(payload.error || "Upload failed.");
    }

    updateImageMessage(previewMessage, {
      sender: window.MEDIGEM_USER.name,
      timestamp,
      imageUrl: payload.image_url,
      serialNumber: payload.serial_number,
      caption: payload.original_name,
      variant: "user"
    });

    registerUploadedImage(payload);

    clearPendingAttachment();
    return payload;
  };

  const buildAutoImageAnalysisMessage = (serialNumber) =>
    `Analyze uploaded image #${serialNumber}.`;

  const startAssistantStream = ({ sender, timestamp }) => {
    const wrapper = renderMessage({
      sender,
      message: "",
      timestamp,
      variant: "assistant",
      streaming: true
    });
    activeAssistantStream = {
      wrapper,
      sender,
      timestamp,
      content: ""
    };
  };

  const appendAssistantDelta = (delta) => {
    if (!activeAssistantStream) {
      startAssistantStream({
        sender: "Atlas",
        timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      });
    }

    activeAssistantStream.content += delta;
    updateMessage(activeAssistantStream.wrapper, {
      sender: activeAssistantStream.sender,
      message: activeAssistantStream.content,
      timestamp: activeAssistantStream.timestamp,
      variant: "assistant",
      streaming: true
    });
  };

  const finishAssistantStream = ({ timestamp }) => {
    if (!activeAssistantStream) {
      return;
    }

    updateMessage(activeAssistantStream.wrapper, {
      sender: activeAssistantStream.sender,
      message: activeAssistantStream.content,
      timestamp: timestamp || activeAssistantStream.timestamp,
      variant: "assistant",
      streaming: false
    });

    activeRunInProgress = false;
    hideLiveAgentStatus();

    if (activeAssistantStream.content) {
      if (voiceInputActive) {
        restartMicAfterSpeech = autoLoopVoice;
        recognition?.stop();
      }
      // Only speak if the user already activated voice via mic or toggle
      if (voiceOutputEnabled) {
        setVoiceStatus("Assistant reply received. Speaking now.");
        speakText(activeAssistantStream.content);
      }
    }

    activeAssistantStream = null;
  };

  if (recognition) {
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = "en-US";

    recognition.addEventListener("start", () => {
      voiceInputActive = true;
      voiceInputTrigger?.setAttribute("aria-pressed", "true");
      voiceInputTrigger?.classList.add("voice-active");
      setVoiceStatus("Listening... speak naturally and your words will appear in the message box.");
      scheduleAutoSendFromSilence();
    });

    recognition.addEventListener("result", (event) => {
      let interimTranscript = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) {
          finalTranscript += `${transcript} `;
        } else {
          interimTranscript += transcript;
        }
      }
      messageInput.value = `${finalTranscript}${interimTranscript}`.trimStart();
      autoResize();
      scheduleAutoSendFromSilence();
    });

    recognition.addEventListener("end", () => {
      voiceInputActive = false;
      clearSilenceTimer();
      voiceInputTrigger?.setAttribute("aria-pressed", "false");
      voiceInputTrigger?.classList.remove("voice-active");
      finalTranscript = messageInput.value ? `${messageInput.value} ` : "";
      if (!restartMicAfterSpeech) {
        setVoiceStatus("Voice input stopped. Press the mic again to continue.");
      }
    });

    recognition.addEventListener("error", (event) => {
      voiceInputActive = false;
      clearSilenceTimer();
      restartMicAfterSpeech = false;
      voiceInputTrigger?.setAttribute("aria-pressed", "false");
      voiceInputTrigger?.classList.remove("voice-active");
      setVoiceStatus(`Voice input error: ${event.error}.`);
    });
  } else {
    voiceInputTrigger?.setAttribute("disabled", "disabled");
    voiceInputTrigger?.classList.add("opacity-50", "cursor-not-allowed");
    setVoiceStatus("Voice input is not supported in this browser.");
  }

  if (!speechSynthesisSupported) {
    voiceOutputToggle?.setAttribute("disabled", "disabled");
    voiceOutputToggle?.classList.add("opacity-50", "cursor-not-allowed");
    if (!recognition) {
      setVoiceStatus("Voice controls are not supported in this browser.");
    }
  }

  socket.addEventListener("open", () => {
    setConnectionState(true);
    statusLabel.textContent = `Connected as ${window.MEDIGEM_USER.roleLabel}.`;
  });

  socket.addEventListener("close", () => {
    setConnectionState(false);
    setComposerBusyState(false);
    if (liveAgentStatus) {
      liveAgentStatus.classList.add("hidden");
    }
    statusLabel.textContent = "Connection closed. Refresh the page to reconnect.";
    if (speechSynthesisSupported) {
      window.speechSynthesis.cancel();
    }
  });

  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);

    if (payload.type === "error") {
      setComposerBusyState(false);
      activeRunInProgress = false;
      if (liveAgentStatus) {
        liveAgentStatus.classList.add("hidden");
      }
      statusLabel.textContent = payload.message;
      return;
    }

    if (payload.type === "status") {
      statusLabel.textContent = payload.message;
      updateLiveAgentStatus(payload);

      if (payload.done && payload.phase === "reasoning") {
        activeRunInProgress = false;
        hideLiveAgentStatus();
      }
      return;
    }

    if (payload.type === "user_echo") {
      renderMessage({ ...payload, variant: "user" });
      return;
    }

    if (payload.type === "assistant") {
      renderMessage({ ...payload, variant: "assistant" });
      return;
    }

    if (payload.type === "assistant_start") {
      startAssistantStream(payload);
      updateLiveAgentStatus({
        phase: "assistant",
        message: "Streaming the assistant response.",
        done: false
      });
      return;
    }

    if (payload.type === "assistant_delta") {
      appendAssistantDelta(payload.delta || "");
      return;
    }

    if (payload.type === "assistant_end") {
      finishAssistantStream(payload);
    }
  });

  chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = messageInput.value.trim();
    const hasAttachment = Boolean(pendingAttachment);
    let uploadedPayload = null;

    if (activeRunInProgress || isSendingAttachment || (!message && !hasAttachment)) {
      return;
    }

    if (hasAttachment) {
      isSendingAttachment = true;
      try {
        uploadedPayload = await uploadPendingAttachment();
      } catch (error) {
        if (attachmentStatus) {
          attachmentStatus.textContent = error.message;
        }
        isSendingAttachment = false;
        return;
      }
      isSendingAttachment = false;
    }

    const finalMessage =
      uploadedPayload && !message
        ? buildAutoImageAnalysisMessage(uploadedPayload.serial_number)
        : message;

    if (finalMessage && socket.readyState === WebSocket.OPEN) {
      resetLiveAgentStatus();
      socket.send(
        JSON.stringify({
          message: finalMessage,
          route_mode: uploadedPayload ? "image_analysis" : "chat",
          image_serial: uploadedPayload ? uploadedPayload.serial_number : null
        })
      );
      messageInput.value = "";
      finalTranscript = "";
      autoResize();
    }
  });

  messageInput.addEventListener("input", autoResize);
  messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      chatForm.requestSubmit();
    }
  });

  voiceInputTrigger?.addEventListener("click", () => {
    if (!recognition) {
      return;
    }

    if (voiceInputActive) {
      autoLoopVoice = false;
      restartMicAfterSpeech = false;
      clearSilenceTimer();
      recognition.stop();
      return;
    }

    // Enable speaker output when user first activates the mic
    setVoiceOutputEnabled(true);
    autoLoopVoice = true;
    finalTranscript = messageInput.value ? `${messageInput.value} ` : "";
    recognition.start();
  });

  voiceOutputToggle?.addEventListener("click", () => {
    if (!speechSynthesisSupported) {
      return;
    }

    if (!voiceOutputEnabled && voiceInputActive) {
      autoLoopVoice = false;
      restartMicAfterSpeech = false;
      clearSilenceTimer();
      recognition?.stop();
    }

    if (voiceOutputEnabled) {
      autoLoopVoice = false;
      restartMicAfterSpeech = false;
    }
    setVoiceOutputEnabled(!voiceOutputEnabled);
    setVoiceStatus(
      voiceOutputEnabled
        ? "Assistant voice playback is on."
        : "Assistant voice playback is off."
    );
  });

  if (attachmentTrigger && attachmentMenu) {
    setAttachmentMenuOpen(false);

    attachmentTrigger.addEventListener("click", () => {
      const isClosed = attachmentMenu.classList.contains("pointer-events-none");
      setAttachmentMenuOpen(isClosed);
    });

    attachmentMenu.addEventListener("click", (event) => {
      const option = event.target.closest("[data-attachment-option]");
      if (!option) {
        return;
      }

      const type = option.getAttribute("data-attachment-option");
      setAttachmentMenuOpen(false);

      if (type === "prescription") {
        prescriptionUpload?.click();
      }
    });

    document.addEventListener("click", (event) => {
      if (
        attachmentMenu.contains(event.target) ||
        attachmentTrigger.contains(event.target)
      ) {
        return;
      }
      setAttachmentMenuOpen(false);
    });
  }

  prescriptionUpload?.addEventListener("change", () => {
    const file = prescriptionUpload.files?.[0];
    if (!file) {
      return;
    }

    setPendingAttachment(file);
  });

  pendingAttachmentRemove?.addEventListener("click", () => {
    clearPendingAttachment();
    if (attachmentStatus) {
      attachmentStatus.textContent = "";
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setAttachmentMenuOpen(false);
      if (voiceInputActive) {
        autoLoopVoice = false;
        restartMicAfterSpeech = false;
        clearSilenceTimer();
        recognition?.stop();
      }
      clearSilenceTimer();
    }
  });
  autoResize();
}
