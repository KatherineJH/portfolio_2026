const progressBar = document.querySelector('.scroll-progress span');
const navToggle = document.querySelector('.nav-toggle');
const navigation = document.querySelector('.site-nav');
const navLinks = [...document.querySelectorAll('.site-nav a')];
const sections = navLinks
  .map((link) => document.querySelector(link.getAttribute('href')))
  .filter(Boolean);

function updateProgress() {
  const scrollable = document.documentElement.scrollHeight - window.innerHeight;
  const progress = scrollable > 0 ? (window.scrollY / scrollable) * 100 : 0;
  progressBar.style.width = `${Math.min(100, Math.max(0, progress))}%`;
}

function updateActiveSection() {
  const marker = window.scrollY + window.innerHeight * 0.35;
  let currentId = sections[0]?.id;
  sections.forEach((section) => {
    if (section.offsetTop <= marker) currentId = section.id;
  });
  navLinks.forEach((link) => {
    const active = link.getAttribute('href') === `#${currentId}`;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'true');
    else link.removeAttribute('aria-current');
  });
}

window.addEventListener('scroll', () => {
  updateProgress();
  updateActiveSection();
}, { passive: true });

navToggle?.addEventListener('click', () => {
  const open = navigation.classList.toggle('open');
  navToggle.setAttribute('aria-expanded', String(open));
});

navLinks.forEach((link) => link.addEventListener('click', () => {
  navigation.classList.remove('open');
  navToggle?.setAttribute('aria-expanded', 'false');
}));

const dialog = document.querySelector('.image-dialog');
const dialogImage = dialog?.querySelector('img');
const dialogCaption = dialog?.querySelector('p');

document.querySelectorAll('.image-button').forEach((button) => {
  button.addEventListener('click', () => {
    if (!dialog || !dialogImage || !dialogCaption) return;
    dialogImage.src = button.dataset.image;
    dialogImage.alt = button.querySelector('img')?.alt || '';
    dialogCaption.textContent = button.dataset.caption || '';
    dialog.showModal();
    document.body.classList.add('dialog-open');
  });
});

function closeDialog() {
  dialog?.close();
  document.body.classList.remove('dialog-open');
}

dialog?.querySelector('.dialog-close')?.addEventListener('click', closeDialog);
dialog?.addEventListener('click', (event) => {
  if (event.target === dialog) closeDialog();
});
dialog?.addEventListener('close', () => document.body.classList.remove('dialog-open'));

updateProgress();
updateActiveSection();
