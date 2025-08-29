function copyText(el){
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges(); sel.addRange(range);
  try { document.execCommand('copy'); } catch (e) {}
  sel.removeAllRanges();
}

function dragDrop(){
  return {
    over:false,
    startLoading(){
      const root = document.querySelector('body');
      if(root && root.__x){ root.__x.$data.loading = true; }
    },
    handleDrop(e){
      this.over=false;
      const dt = e.dataTransfer;
      if(dt && dt.files && dt.files.length){
        const form = document.getElementById('composeForm');
        const input = form.querySelector('input[name="files"]'); // MULTIPLO
        input.files = dt.files;
        const root = document.querySelector('body');
        if(root && root.__x){ root.__x.$data.loading = true; }
        form.submit();
      }
    }
  }
}
