import os
import webbrowser
import threading
import uvicorn
if __name__ == '__main__':
    port = int(os.getenv('PORT', '8766'))
    if not os.getenv('RENDER'):
        threading.Timer(1.5, lambda: webbrowser.open(f'http://127.0.0.1:{port}')).start()
    uvicorn.run('backend.app:app', host='0.0.0.0' if os.getenv('RENDER') else '127.0.0.1', port=port, workers=1, access_log=False)
