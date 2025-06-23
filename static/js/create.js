document.addEventListener('DOMContentLoaded', () => {
    const editor = document.getElementById('lesson-editor');
    const lessonForm = document.getElementById('lesson-form');
    const scriptInput = document.getElementById('script-input');
    
    // --- Button and Input Elements ---
    const addImageBtn = document.getElementById('add-image-btn');
    const addAudioBtn = document.getElementById('add-audio-btn'); // New Audio Button
    const mediaUploadInput = document.getElementById('media-upload-input'); // Corrected ID

    // This will hold the files to be submitted with the final form
    const fileStore = new DataTransfer();

    // --- Event Listeners ---

    // 1. Handle "Add Image" button click
    if (addImageBtn) {
        addImageBtn.addEventListener('click', () => {
            // Set the input to only accept images and trigger a click
            mediaUploadInput.accept = 'image/*';
            mediaUploadInput.click();
        });
    }

    // 2. Handle "Add Audio" button click
    if (addAudioBtn) {
        addAudioBtn.addEventListener('click', () => {
            // Set the input to only accept audio and trigger a click
            mediaUploadInput.accept = 'audio/*';
            mediaUploadInput.click();
        });
    }

    // 3. Handle the file selection from the unified input
    if (mediaUploadInput) {
        mediaUploadInput.addEventListener('change', (event) => {
            const file = event.target.files[0];
            if (file) {
                // Add the file to our store for final submission
                fileStore.items.add(file);
                
                // Insert a tag and a visual preview into the editor based on file type
                if (file.type.startsWith('image/')) {
                    insertImageTagInEditor(file);
                } else if (file.type.startsWith('audio/')) {
                    insertAudioTagInEditor(file);
                }
            }
            // Clear the input so the same file can be added again if needed
            event.target.value = '';
        });
    }

    // 4. Before submitting, clean up the editor content for the parser
    if (lessonForm) {
        lessonForm.addEventListener('submit', (event) => {
            const editorHtmlInput = document.getElementById('editor-html-input');

            // --- NEW: Save the raw HTML *before* stripping it ---
            if (editorHtmlInput) {
                editorHtmlInput.value = editor.innerHTML;
            }
            const tempDiv = document.createElement('div');
            tempDiv.innerHTML = editor.innerHTML;
            
            // Remove the visual previews (img and audio tags) before submission
            tempDiv.querySelectorAll('img, audio, .audio-preview').forEach(el => el.remove());
            
            // Convert breaks to newlines and strip remaining HTML
            let scriptText = tempDiv.innerHTML.replace(/<br\s*[\/]?>/gi, "\n");
            scriptText = scriptText.replace(/<p><\/p>/g, ''); // Remove empty paragraphs
            scriptText = scriptText.replace(/ /g, ' '); // Replace non-breaking spaces
            scriptText = scriptText.replace(/<[^>]*>?/gm, ''); // Strip all tags

            scriptInput.value = scriptText.trim();

            // Attach the collected files to the hidden file input for submission
            mediaUploadInput.files = fileStore.files;
        });
    }


    // --- Helper Functions to insert content into the editor ---

    function insertImageTagInEditor(file) {
        const altText = prompt("Please enter a short description for this image (for screen readers):", file.name);
        if (altText === null) {
            fileStore.items.remove(fileStore.items.length - 1);
            return;
        }

        const imageTag = `[IMAGE: alt="${altText}"]`;
        const previewUrl = URL.createObjectURL(file);
        const previewImg = document.createElement('img');
        previewImg.src = previewUrl;
        previewImg.alt = altText;
        previewImg.style.maxWidth = '200px';
        previewImg.style.display = 'block';
        previewImg.style.margin = '10px 0';
        previewImg.setAttribute('contenteditable', 'false');

        insertNodeAtCursor(document.createTextNode(imageTag));
        insertNodeAtCursor(document.createElement('br'));
        insertNodeAtCursor(previewImg);
        insertNodeAtCursor(document.createElement('br'));
    }

    function insertAudioTagInEditor(file) {
        const description = prompt("Please enter a short description for this audio clip:", file.name);
        if (description === null) {
            fileStore.items.remove(fileStore.items.length - 1);
            return;
        }

        const audioTag = `[AUDIO: description="${description}"]`;
        const previewUrl = URL.createObjectURL(file);
        
        // Create a more descriptive preview element for audio
        const previewDiv = document.createElement('div');
        previewDiv.className = 'audio-preview';
        previewDiv.style.border = '1px solid #ccc';
        previewDiv.style.padding = '10px';
        previewDiv.style.margin = '10px 0';
        previewDiv.style.borderRadius = '5px';
        previewDiv.setAttribute('contenteditable', 'false');
        
        const audio = document.createElement('audio');
        audio.controls = true;
        audio.src = previewUrl;
        
        previewDiv.innerText = `Audio Clip: ${description}`;
        previewDiv.appendChild(audio);

        insertNodeAtCursor(document.createTextNode(audioTag));
        insertNodeAtCursor(document.createElement('br'));
        insertNodeAtCursor(previewDiv);
        insertNodeAtCursor(document.createElement('br'));
    }

    function insertNodeAtCursor(node) {
        const selection = window.getSelection();
        if (selection.getRangeAt && selection.rangeCount) {
            const range = selection.getRangeAt(0);
            range.deleteContents();
            range.insertNode(node);
            
            // Move cursor after the inserted node
            const newRange = document.createRange();
            newRange.setStartAfter(node);
            newRange.collapse(true);
            selection.removeAllRanges();
            selection.addRange(newRange);
        }
    }
});