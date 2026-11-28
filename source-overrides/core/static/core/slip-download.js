'use strict';

const slipFormats = {
  pdf: {mime: 'application/pdf', signature: [37, 80, 68, 70, 45]},
  png: {mime: 'image/png', signature: [137, 80, 78, 71, 13, 10, 26, 10]},
};

for (const group of document.querySelectorAll('[data-slip-download]')) {
  const status = group.querySelector('[role="status"]');
  let busy = false;

  for (const button of group.querySelectorAll('button[data-slip-format]')) {
    const format = button.dataset.slipFormat;
    const {mime, signature} = slipFormats[format];
    const label = button.textContent;
    button.addEventListener('click', async () => {
      if (busy) return;
      busy = true;
      button.disabled = true;
      button.textContent = 'Menyiapkan ' + format.toUpperCase() + '…';
      status.textContent = '';
      try {
        const url = new URL(group.dataset.slipDownload, location.href);
        url.searchParams.set('format', format);
        const response = await fetch(url, {
          credentials: 'same-origin',
          cache: 'no-store',
          redirect: 'follow',
          headers: {Accept: mime},
        });
        if (response.redirected || response.status === 401 || response.status === 403) {
          status.textContent = 'Sesi masuk perlu diperbarui. Masuk kembali, lalu coba lagi.';
          return;
        }
        if (!response.ok || response.headers.get('Content-Type')?.split(';')[0] !== mime) {
          throw new Error('Unexpected file type');
        }
        const file = await response.blob();
        const header = new Uint8Array(await file.slice(0, signature.length).arrayBuffer());
        if (!signature.every((byte, index) => byte === header[index])) {
          throw new Error('Invalid file signature');
        }
        const disposition = response.headers.get('Content-Disposition') || '';
        const name = disposition.match(/filename="([^"]+)"/i)?.[1];
        const objectUrl = URL.createObjectURL(file);
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = name?.toLowerCase().endsWith('.' + format) ? name : 'slipgaji.' + format;
        link.hidden = true;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
      } catch {
        status.textContent = 'File ' + format.toUpperCase() + ' belum bisa diunduh. Coba lagi.';
      } finally {
        busy = false;
        button.disabled = false;
        button.textContent = label;
      }
    });
    button.disabled = false;
  }
}
