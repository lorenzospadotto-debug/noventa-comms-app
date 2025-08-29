function copyText(el){
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  try { document.execCommand('copy'); } catch (e) {}
  sel.removeAllRanges();
}

function dragDrop(){
  return {
    over:false,
    handleDrop(e){
      this.over=false;
      const dt = e.dataTransfer;
      if(dt && dt.files && dt.files.length){
        const form = document.getElementById('uploadForm');
        const input = form.querySelector('input[name="file"]');
        input.files = dt.files;
        form.submit();
      }
    },
    submitOnChange(e){
      const form = document.getElementById('uploadForm');
      form.submit();
    }
  }
}
