document.addEventListener('DOMContentLoaded', () => {
    const chatBox = document.getElementById('chat-box');
    const inputArea = document.getElementById('input-area');
    const systemMessage = document.getElementById('system-message');
    const qnaInput = document.getElementById('qna-input');
    const sendQnaBtn = document.getElementById('send-qna-btn');
    const resetBtn = document.getElementById('reset-btn');
    const deleteLastBtn = document.getElementById('delete-last-btn');

    let isWaitingForResponse = false;

    // --- Q&A Input Logic ---
    async function sendQuestion() {
        if (isWaitingForResponse) return;
        const question = qnaInput.value.trim();
        if (question === "") return;

        addMessage(question, 'student');
        qnaInput.value = '';
        isWaitingForResponse = true;

        // 1. Call the new intent classifier route first
        const intentResponse = await fetch('/chat/intent', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' ,'X-CSRF-TOKEN': CSRF_TOKEN},
            body: JSON.stringify({ user_input: question, lesson_id: LESSON_ID })
        });
        const intentData = await intentResponse.json();

        isWaitingForResponse = false;

        // 2. Call postToChat with the classified intent
        if (intentData.intent === 'MEDIA_REQUEST') {
            postToChat(intentData.alt_text, 'MEDIA_REQUEST');
        } else {
            postToChat(intentData.query, 'QNA');
        }
    }

    sendQnaBtn.addEventListener('click', sendQuestion);
    qnaInput.addEventListener('keypress', function(event) {
        if (event.key === 'Enter') {
            event.preventDefault();
            sendQuestion();
        }
    });

    // --- Chat Control Button Logic ---
    if (resetBtn) {
        resetBtn.addEventListener('click', () => {
            if (isWaitingForResponse) return;
            if (confirm('Are you sure you want to reset this entire conversation? Your progress in this chapter will be lost.')) {
                isWaitingForResponse = true;
                systemMessage.innerText = 'Resetting...';
                systemMessage.style.display = 'block';
                fetch('/chat/reset', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' ,'X-CSRF-TOKEN': CSRF_TOKEN},
                    body: JSON.stringify({ lesson_id: LESSON_ID })
                }).then(res => res.json()).then(data => {
                    if (data.success) {
                        window.location.reload();
                    } else {
                        alert('Could not reset conversation.');
                        isWaitingForResponse = false;
                        systemMessage.style.display = 'none';
                    }
                });
            }
        });
    }

    if (deleteLastBtn) {
        deleteLastBtn.addEventListener('click', async () => {
            if (isWaitingForResponse) return;
            isWaitingForResponse = true;
            systemMessage.innerText = 'Deleting...';
            systemMessage.style.display = 'block';

            const response = await fetch('/chat/delete_last_turn', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json','X-CSRF-TOKEN': CSRF_TOKEN },
                body: JSON.stringify({ lesson_id: LESSON_ID })
            });

            const data = await response.json();
            if (data.success) {
                window.location.reload();
            } else {
                alert(data.message || 'Could not delete the last turn.');
            }

            isWaitingForResponse = false;
            systemMessage.style.display = 'none';
        });
    }
    
    // --- Rendering and UI Functions ---
    function addMessage(text, sender) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${sender}-message`;
        messageDiv.innerHTML = marked.parse(text);
        chatBox.appendChild(messageDiv);
        chatBox.scrollTop = chatBox.scrollHeight;
        MathJax.typesetPromise([messageDiv]).catch((err) => console.log('MathJax error:', err));
    }

    function addImageMessage(url, alt) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message tutor-message media-message';
        const img = document.createElement('img');
        img.src = url;
        img.alt = alt;
        messageDiv.appendChild(img);
        chatBox.appendChild(messageDiv);
        chatBox.scrollTop = chatBox.scrollHeight;
    }

    function addAudioMessage(url, description) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message tutor-message audio-message';
        const audio = document.createElement('audio');
        audio.controls = true;
        audio.src = url;
        messageDiv.appendChild(audio);
        chatBox.appendChild(messageDiv);
        chatBox.scrollTop = chatBox.scrollHeight;
    }

    function showMCQOptions(questionData) {
        inputArea.innerHTML = '';
        const questionText = document.createElement('p');
        questionText.innerText = questionData.question;
        inputArea.appendChild(questionText);
        const optionsContainer = document.createElement('div');
        optionsContainer.className = 'options-container';
        for (const key in questionData.options) {
            const button = document.createElement('button');
            button.className = 'btn btn-secondary';
            button.innerText = `${key}: ${questionData.options[key]}`;
            button.dataset.answer = key;
            button.addEventListener('click', () => {
                if (isWaitingForResponse) return;
                const answerText = `${key}: ${questionData.options[key]}`;
                addMessage(answerText, 'student');
                postToChat(key, 'LESSON_FLOW');
            });
            optionsContainer.appendChild(button);
        }
        inputArea.appendChild(optionsContainer);
    }

    function showShortAnswerInput(questionData) {
        inputArea.innerHTML = '';
        const questionText = document.createElement('p');
        questionText.innerText = questionData.question;
        inputArea.appendChild(questionText);
        const answerTextarea = document.createElement('textarea');
        answerTextarea.rows = 3;
        answerTextarea.placeholder = "Type your answer here...";
        inputArea.appendChild(answerTextarea);
        const submitButton = document.createElement('button');
        submitButton.className = 'btn';
        submitButton.innerText = 'Submit Answer';
        submitButton.addEventListener('click', () => {
            if (isWaitingForResponse) return;
            const answer = answerTextarea.value.trim();
            if (answer === "") { alert("Please type an answer."); return; }
            addMessage(answer, 'student');
            postToChat(answer, 'LESSON_FLOW');
        });
        inputArea.appendChild(submitButton);
    }
    
    function showContinueButton() {
        inputArea.innerHTML = '';
        const buttonContainer = document.createElement('div');
        buttonContainer.style.textAlign = 'right';
        const continueButton = document.createElement('button');
        continueButton.innerText = 'Continue';
        continueButton.className = 'btn btn-primary'; 
        continueButton.addEventListener('click', () => {
            if (isWaitingForResponse) return;
            postToChat('Continue', 'LESSON_FLOW');
        });
        buttonContainer.appendChild(continueButton);
        inputArea.appendChild(buttonContainer);
    }

    // --- Core Chat Function ---
    async function postToChat(userInput = null, requestType = 'LESSON_FLOW') {
        isWaitingForResponse = true;
        systemMessage.innerText = 'Guidee is thinking...';
        systemMessage.style.display = 'block';
        qnaInput.disabled = true;
        sendQnaBtn.disabled = true;
        inputArea.innerHTML = ''; 

        const response = await fetch('/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' ,'X-CSRF-TOKEN': CSRF_TOKEN},
            body: JSON.stringify({ lesson_id: LESSON_ID, user_input: userInput, request_type: requestType })
        });
        const data = await response.json();

        isWaitingForResponse = false;
        systemMessage.style.display = 'none';
        qnaInput.disabled = false;
        sendQnaBtn.disabled = false;
        
        if (data.tutor_text) {
             addMessage(data.tutor_text, 'tutor');
        }
        if (data.media_url) {
            if (data.media_type === 'audio') {
                addAudioMessage(data.media_url, "Listen to the clip above:"); 
            } else {
                addImageMessage(data.media_url, "View the image above");
            }
        }

        if (data.is_qna_response) {
             showContinueButton();
             return; 
        }

        if (data.is_lesson_end) {
            inputArea.innerHTML = '';
            if (data.certificate_url) {
                const certLink = document.createElement('a');
                certLink.href = data.certificate_url;
                certLink.innerText = 'View Your Certificate!';
                certLink.className = 'btn btn-primary';
                inputArea.appendChild(certLink);
            } else if (data.next_chapter_url) {
                const nextChapterButton = document.createElement('a');
                nextChapterButton.href = data.next_chapter_url;
                nextChapterButton.innerText = 'Go to Next Chapter';
                nextChapterButton.className = 'btn btn-primary';
                inputArea.appendChild(nextChapterButton);
            }
        } else if (data.question) {
            if (data.question.type === 'QUESTION_MCQ') {
                showMCQOptions(data.question);
            } else if (data.question.type === 'QUESTION_SA') {
                showShortAnswerInput(data.question);
            }
        } else {
            showContinueButton();
        }
    }

    // --- Initialization Logic ---
    function initializeLesson() {
        if (initialHistoryRecord && initialHistoryRecord.history_json) {
            try {
                const history = JSON.parse(initialHistoryRecord.history_json);
                history.forEach(message => {
                    if (message.type === 'text') {
                        addMessage(message.content, message.sender);
                    } else if (message.type === 'image') {
                        addImageMessage(message.url, message.alt);
                    } else if (message.type === 'audio') {
                        addAudioMessage(message.url, message.alt);
                    }
                });
            } catch (e) {
                console.error("Could not parse chat history:", e);
            }
        }
        
        // Always call postToChat. The backend will determine the correct next step,
        // whether it's the next message, the end-of-lesson UI, or resuming from a saved state.
        postToChat(null, 'LESSON_FLOW');
    }

    initializeLesson();
});

// --- Lightbox/Modal Logic ---
document.addEventListener('DOMContentLoaded', () => {
    const modal = document.getElementById("image-modal");
    if (!modal) return;

    const modalImg = document.getElementById("modal-img");
    const closeBtn = document.querySelector(".close-btn");
    const chatBox = document.getElementById('chat-box');

    chatBox.addEventListener('click', function(event) {
        if (event.target.tagName === 'IMG' && event.target.closest('.media-message')) {
            modal.style.display = "block";
            modalImg.src = event.target.src;
        }
    });

    function closeModal() {
        modal.style.display = "none";
    }

    if(closeBtn) {
        closeBtn.onclick = closeModal;
    }
    
    window.onclick = function(event) {
        if (event.target == modal) {
            closeModal();
        }
    }
});