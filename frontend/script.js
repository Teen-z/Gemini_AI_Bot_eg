const form = document.getElementById("chatForm");
const input = document.getElementById("messageInput");
const chatArea = document.getElementById("chatArea");
const sendButton = document.getElementById("sendButton");
const welcome = document.getElementById("welcome");

// สร้าง Session ID ประจำแท็บเบราว์เซอร์เพื่อให้คุยต่อเนื่องได้โดยประวัติไม่ปนกับคนอื่น
let sessionId = sessionStorage.getItem("chat_session_id");
if (!sessionId) {
    sessionId = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : "sess_" + Date.now() + "_" + Math.random().toString(36).substring(2, 9);
    sessionStorage.setItem("chat_session_id", sessionId);
}

function addMessage(text, type) {
    const message = document.createElement("div");
    message.className = "chat-message " +
        (type === "user" ? "user-message" : "ai-message");

    if (type === "ai") {
        const label = document.createElement("div");
        label.className = "ai-label";
        label.textContent = "✦ AI Assistant";
        message.appendChild(label);
    }

    const content = document.createElement("div");
    content.textContent = text;

    message.appendChild(content);
    chatArea.appendChild(message);

    window.scrollTo({
        top: document.body.scrollHeight,
        behavior: "smooth"
    });

    return message;
}

async function sendMessage(text) {
    text = text.trim();
    if (!text || sendButton.disabled) return;

    welcome.style.display = "none";
    addMessage(text, "user");

    input.value = "";
    input.style.height = "auto";
    sendButton.disabled = true;

    const loading = addMessage("กำลังคิดคำตอบ...", "ai");
    loading.querySelector("div:last-child").classList.add("loading");

    try {
        const response = await fetch("/api/chat", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                message: text,
                session_id: sessionId
            })
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || "เกิดข้อผิดพลาด");
        }

        loading.querySelector("div:last-child").textContent = data.answer;
        loading.querySelector("div:last-child").classList.remove("loading");

    } catch (error) {
        loading.querySelector("div:last-child").textContent = "ขออภัย: " + error.message;
        loading.querySelector("div:last-child").classList.remove("loading");
    } finally {
        sendButton.disabled = false;
        input.focus();
    }
}

form.addEventListener("submit", function(event) {
    event.preventDefault();
    sendMessage(input.value);
});

input.addEventListener("input", function() {
    this.style.height = "auto";
    this.style.height = Math.min(this.scrollHeight, 150) + "px";
});

input.addEventListener("keydown", function(event) {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        form.requestSubmit();
    }
});

document.querySelectorAll(".suggestion").forEach(button => {
    button.addEventListener("click", function() {
        sendMessage(this.dataset.question);
    });
});