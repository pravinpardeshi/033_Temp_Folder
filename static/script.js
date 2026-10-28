let sessionId = null;
const userInput = document.getElementById('userInput');
const sendBtn = document.getElementById('sendBtn');
const messageContainer = document.getElementById('messageContainer');
const newChatBtn = document.getElementById('newChatBtn');

userInput.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = (this.scrollHeight) + 'px';
});

async function sendMessage() {
    const text = userInput.value.trim();
    if (!text) return;

    const welcome = document.querySelector('.welcome-screen');
    if (welcome) welcome.remove();

    appendMessage('user', text);
    userInput.value = '';
    userInput.style.height = 'auto';
    
    sendBtn.disabled = true;
    const aiMsgDiv = appendMessage('ai', '...');

    try {
        const response = await fetch('/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: text,
                session_id: sessionId
            })
        });

        const data = await response.json();
        sessionId = data.session_id;
        aiMsgDiv.querySelector('.msg-content').innerText = data.response;
    } catch (error) {
        aiMsgDiv.querySelector('.msg-content').innerText = "Error: Could not connect to server.";
    } finally {
        sendBtn.disabled = false;
    }
}

function appendMessage(role, text) {
    const wrapper = document.createElement('div');
    wrapper.className = `msg-wrapper ${role}`;
    
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.innerText = role === 'user' ? 'U' : 'AI';
    
    const content = document.createElement('div');
    content.className = 'msg-content';
    content.innerText = text;
    
    wrapper.appendChild(avatar);
    wrapper.appendChild(content);
    messageContainer.appendChild(wrapper);
    
    messageContainer.scrollTop = messageContainer.scrollHeight;
    return wrapper;
}

sendBtn.addEventListener('click', sendMessage);
userInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

newChatBtn.addEventListener('click', () => {
    sessionId = null;
    messageContainer.innerHTML = '<div class="welcome-screen"><h1>How can I help you today?</h1></div>';
});
