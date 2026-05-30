/* ===== user manual loader (extracted) =====
   Loaded on pages that should show onboarding (login + chat).
*/

(() => {
  const COOKIE_NAME = "medigem_manual_shown";
  const COOKIE_DAYS = 3650; // ~10 years

  const getCookie = (name) => {
    const v = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return v ? v.pop() : null;
  };

  const setCookie = (name, value, days) => {
    const expires = new Date(Date.now() + days * 864e5).toUTCString();
    document.cookie = `${name}=${value}; expires=${expires}; path=/; SameSite=Lax`;
  };

  const modal = document.getElementById('user-manual-modal');
  if (!modal) return;
  const overlay = document.getElementById('user-manual-overlay');
  const imgEl = document.getElementById('manual-image');
  const stepTitle = document.getElementById('manual-step-title');
  const stepDesc = document.getElementById('manual-step-desc');
  const progressText = document.getElementById('manual-progress');
  const prevBtn = document.getElementById('manual-prev');
  const nextBtn = document.getElementById('manual-next');
  const closeBtn = document.getElementById('manual-close');
  const titleEl = document.getElementById('manual-title');

  let slides = [];
  let idx = 0;

  const hideModal = (persist = true) => {
    modal.classList.add('hidden');
    modal.setAttribute('aria-hidden', 'true');
    if (persist) setCookie(COOKIE_NAME, 'true', COOKIE_DAYS);
  };

  const showModal = () => {
    modal.classList.remove('hidden');
    modal.setAttribute('aria-hidden', 'false');
    // trap focus minimally
    nextBtn.focus();
  };

  const updateControls = () => {
    progressText.textContent = `${idx + 1} / ${slides.length}`;
    prevBtn.disabled = idx === 0;
    if (idx === slides.length - 1) {
      nextBtn.textContent = 'Done';
    } else {
      nextBtn.textContent = 'Next';
    }
  };

  const showSlide = (i) => {
    if (!slides[i]) return;
    idx = i;
    // transition image
    imgEl.classList.add('fading');
    setTimeout(() => {
      imgEl.src = slides[i].image;
      imgEl.alt = slides[i].title || 'Manual image';
      imgEl.classList.remove('fading');
    }, 160);
    stepTitle.textContent = slides[i].title || '';
    stepDesc.textContent = slides[i].desc || '';
    updateControls();
  };

  prevBtn?.addEventListener('click', () => {
    if (idx > 0) showSlide(idx - 1);
  });

  nextBtn?.addEventListener('click', () => {
    if (idx < slides.length - 1) {
      showSlide(idx + 1);
    } else {
      hideModal(true);
    }
  });

  closeBtn?.addEventListener('click', () => hideModal(true));
  overlay?.addEventListener('click', () => hideModal(true));

  // keyboard navigation
  window.addEventListener('keydown', (e) => {
    if (modal.classList.contains('hidden')) return;
    if (e.key === 'ArrowRight') nextBtn.click();
    if (e.key === 'ArrowLeft') prevBtn.click();
    if (e.key === 'Escape') hideModal(true);
  });

  const tryLoadManifest = async () => {
    try {
      const res = await fetch('/static/manual/manifest.json', { cache: 'no-cache' });
      if (!res.ok) throw new Error('Manifest not found');
      const data = await res.json();
      if (!Array.isArray(data) || data.length === 0) throw new Error('Empty manifest');
      // map filenames to static path if they look like bare filenames
      slides = data.map((s) => {
        const image = s.image && !s.image.startsWith('/') ? `/static/manual/${s.image}` : s.image;
        return { image, title: s.title || '', desc: s.desc || '' };
      });
      titleEl.textContent = data.title || 'Welcome';
      showSlide(0);
      showModal();
    } catch (err) {
      // fallback minimal slide if manifest missing
      slides = [
        { image: '/static/manual/placeholder.png', title: 'Welcome', desc: 'Welcome to the app. Place your manual images in /static/manual and edit manifest.json.' }
      ];
      showSlide(0);
      showModal();
      console.warn('User manual manifest load failed:', err);
    }
  };

  // Only show if cookie not set
  if (!getCookie(COOKIE_NAME)) {
    tryLoadManifest();
  }
})();
