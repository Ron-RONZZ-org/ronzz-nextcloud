document.addEventListener('DOMContentLoaded', () => {
    const grid = document.querySelector('.button-grid');
    if (!grid) return;

    const links = grid.querySelectorAll('.nav-button');

    links.forEach(link => {
        link.addEventListener('click', (e) => {
            // Ignore clic molette / ctrl+clic / cmd+clic (ouverture nouvel onglet)
            if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) {
                return;
            }

            // Boutons externes (target="_blank") : la page ne navigue pas, donc
            // pageshow ne sera jamais déclenché et le spinner resterait figé.
            if (link.target === '_blank') {
                return;
            }

            // Fige la grille et affiche le spinner sur le bouton cliqué
            grid.classList.add('is-frozen');
            document.body.classList.add('portal-waiting');
            link.classList.add('is-loading');

            // La navigation suit son cours normalement (pas de preventDefault)
        });
    });

    // Sécurité : si l'utilisateur revient en arrière (bouton précédent du navigateur),
    // on dégèle la page pour éviter qu'elle reste bloquée visuellement.
    window.addEventListener('pageshow', (event) => {
        if (event.persisted) {
            grid.classList.remove('is-frozen');
            document.body.classList.remove('portal-waiting');
            links.forEach(l => l.classList.remove('is-loading'));
        }
    });
});