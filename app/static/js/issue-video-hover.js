/**
 * Клик по превью видео (data-video-hover-src) — плавающее окно с полноценным плеером.
 * Повторный клик по тому же превью закрывает окно. Клик вне окна — закрыть. Escape — закрыть.
 */
(function () {
  let pop;
  let popVideo;
  let activeThumb = null;

  function ensurePop() {
    if (pop) return;
    pop = document.createElement('div');
    pop.id = 'issue-video-hover-pop';
    pop.className =
      'issue-video-hover-pop card bg-dark border border-secondary border-opacity-50 shadow-lg';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-hidden', 'true');
    pop.innerHTML =
      '<div class="position-relative">' +
      '<button type="button" class="btn-close btn-close-white position-absolute top-0 end-0 m-2 issue-video-hover-close" style="z-index: 1" aria-label="Закрыть"></button>' +
      '<div class="card-body p-2 pt-4">' +
      '<video controls playsinline class="w-100 rounded"></video>' +
      '</div></div>';
    popVideo = pop.querySelector('video');
    var closeBtn = pop.querySelector('.issue-video-hover-close');
    closeBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      hide();
    });
    document.body.appendChild(pop);
  }

  function positionNear(thumb) {
    var r = thumb.getBoundingClientRect();
    var mw = Math.min(720, window.innerWidth * 0.92);
    var left = r.left + r.width / 2 - mw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
    var gap = 8;
    var below = r.bottom + gap;
    var estH = Math.min(window.innerHeight * 0.72, 480);
    var top = below;
    if (below + estH > window.innerHeight - 8) {
      top = r.top - gap - estH;
    }
    if (top < 8) top = 8;
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
    pop.style.width = mw + 'px';
  }

  function showFor(thumb) {
    var src = thumb.getAttribute('data-video-hover-src');
    if (!src) return;
    ensurePop();
    activeThumb = thumb;
    if (popVideo.getAttribute('src') !== src) {
      popVideo.src = src;
    }
    positionNear(thumb);
    pop.classList.add('is-visible');
    pop.setAttribute('aria-hidden', 'false');
    popVideo.play().catch(function () {});
  }

  function hide() {
    if (!pop) return;
    pop.classList.remove('is-visible');
    pop.setAttribute('aria-hidden', 'true');
    popVideo.pause();
    popVideo.removeAttribute('src');
    popVideo.load();
    activeThumb = null;
  }

  document.addEventListener('click', function (e) {
    var thumb = e.target.closest('[data-video-hover-src]');
    if (thumb) {
      e.stopPropagation();
      if (pop && pop.classList.contains('is-visible') && activeThumb === thumb) {
        hide();
        return;
      }
      showFor(thumb);
      return;
    }
    if (!pop || !pop.classList.contains('is-visible')) return;
    if (pop.contains(e.target)) return;
    hide();
  });

  document.addEventListener(
    'scroll',
    function () {
      if (pop && pop.classList.contains('is-visible')) hide();
    },
    true
  );

  window.addEventListener('resize', hide);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && pop && pop.classList.contains('is-visible')) hide();
  });
})();
