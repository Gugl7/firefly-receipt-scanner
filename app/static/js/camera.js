let stream;
const video = document.getElementById('camera-preview');
const canvas = document.getElementById('canvas');
const startButton = document.getElementById('startCamera');
const captureButton = document.getElementById('capture');
const retakeButton = document.getElementById('retake');
const fileInput = document.getElementById('files') || document.getElementById('file');
const form = document.getElementById('receipt-form');

if (startButton) {
    startButton.addEventListener('click', async () => {
        try {
            stream = await navigator.mediaDevices.getUserMedia({
                video: { facingMode: 'environment' }
            });
            video.srcObject = stream;
            video.style.display = 'block';
            startButton.style.display = 'none';
            captureButton.style.display = 'block';
            fileInput.disabled = true;
        } catch (err) {
            console.error('Error accessing camera:', err);
            alert('Could not access the camera. Please check permissions or use file upload instead.');
        }
    });
}

if (captureButton) {
    captureButton.addEventListener('click', () => {
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext('2d').drawImage(video, 0, 0);
        
        // Convert the canvas to a blob
        canvas.toBlob((blob) => {
            const file = new File([blob], 'receipt.jpg', { type: 'image/jpeg' });
            const dataTransfer = new DataTransfer();
            dataTransfer.items.add(file);
            fileInput.files = dataTransfer.files;
            
            // Stop the camera stream
            stream.getTracks().forEach(track => track.stop());
            video.style.display = 'none';
            captureButton.style.display = 'none';
            retakeButton.style.display = 'block';
            fileInput.disabled = false;
        }, 'image/jpeg', 0.8);
    });
}

if (retakeButton) {
    retakeButton.addEventListener('click', () => {
        video.style.display = 'none';
        startButton.style.display = 'block';
        retakeButton.style.display = 'none';
        fileInput.value = '';
    });
}

// Clean up on page unload
window.addEventListener('unload', () => {
    if (stream) {
        stream.getTracks().forEach(track => track.stop());
    }
}); 