class SearchableSelect {
    constructor(selectElement) {
        this.select = selectElement;
        this.optionsList = null;
        this.buildUI();
    }

    buildUI() {
        this.wrapper = document.createElement('div');
        this.wrapper.className = 'custom-select-wrapper';

        // Inherit width constraints from original select
        if (this.select.style.minWidth) this.wrapper.style.minWidth = this.select.style.minWidth;
        if (this.select.style.width) this.wrapper.style.width = this.select.style.width;
        if (this.select.style.maxWidth) this.wrapper.style.maxWidth = this.select.style.maxWidth;

        this.select.parentNode.insertBefore(this.wrapper, this.select);
        this.wrapper.appendChild(this.select);

        this.trigger = document.createElement('div');
        this.trigger.className = 'custom-select-trigger';

        this.dropdown = document.createElement('div');
        this.dropdown.className = 'custom-select-dropdown';

        this.searchContainer = document.createElement('div');
        this.searchContainer.className = 'custom-select-search';
        this.searchInput = document.createElement('input');
        this.searchInput.type = 'text';
        this.searchInput.placeholder = 'Search...';
        this.searchContainer.appendChild(this.searchInput);

        this.optionsList = document.createElement('div');
        this.optionsList.className = 'custom-select-options';

        this.dropdown.appendChild(this.searchContainer);
        this.dropdown.appendChild(this.optionsList);
        this.wrapper.appendChild(this.trigger);
        this.wrapper.appendChild(this.dropdown);

        this.trigger.addEventListener('click', (e) => this.toggle(e));
        this.searchInput.addEventListener('input', () => this.filterOptions());
        this.searchInput.addEventListener('click', (e) => e.stopPropagation());

        document.addEventListener('click', (e) => {
            if (!this.wrapper.contains(e.target)) this.close();
        });

        this.refresh();
    }

    updateDisabledState() {
        if (this.select.disabled) {
            this.trigger.style.opacity = '0.5';
            this.trigger.style.cursor = 'not-allowed';
            this.wrapper.style.pointerEvents = 'none';
        } else {
            this.trigger.style.opacity = '1';
            this.trigger.style.cursor = 'pointer';
            this.wrapper.style.pointerEvents = 'auto';
        }
    }

    refresh() {
        this.optionsList.innerHTML = '';
        const options = Array.from(this.select.options);
        
        const selectedOpt = options.find(o => o.selected) || options[0];
        this.trigger.innerHTML = `<span>${selectedOpt ? selectedOpt.textContent : '— Select —'}</span><i class="fa-solid fa-chevron-down"></i>`;
        
        options.forEach((opt, index) => {
            // Skip hidden options
            if (opt.style.display === 'none') return;

            const div = document.createElement('div');
            div.className = 'custom-option';
            
            // Keep the placeholder visually distinct/faded if desired.
            if (opt.value === '') div.style.opacity = '0.6';
            
            if (opt.selected) div.classList.add('selected');
            div.textContent = opt.textContent;
            div.dataset.value = opt.value;
            
            div.addEventListener('click', (e) => {
                e.stopPropagation();
                this.select.selectedIndex = index;
                this.close();
                this.refresh();  // re-render so .selected class moves to the new option
                this.select.dispatchEvent(new Event('change'));
            });
            
            this.optionsList.appendChild(div);
        });
        this.updateDisabledState();
    }

    toggle(e) {
        e.stopPropagation();
        if (this.select.disabled) return;
        
        document.querySelectorAll('.custom-select-dropdown.open').forEach(d => {
            if (d !== this.dropdown) d.classList.remove('open');
        });
        document.querySelectorAll('.custom-select-trigger.open').forEach(t => {
            if (t !== this.trigger) t.classList.remove('open');
        });

        const isOpen = this.dropdown.classList.contains('open');
        if (isOpen) {
            this.close();
        } else {
            this.open();
        }
    }

    open() {
        this.dropdown.classList.add('open');
        this.trigger.classList.add('open');
        this.searchInput.value = '';
        this.filterOptions();
        setTimeout(() => this.searchInput.focus(), 50);
    }

    close() {
        this.dropdown.classList.remove('open');
        this.trigger.classList.remove('open');
    }

    filterOptions() {
        const query = this.searchInput.value.toLowerCase();
        let hasVisible = false;
        
        Array.from(this.optionsList.children).forEach(child => {
            if (child.classList.contains('no-results')) return;
            
            const text = child.textContent.toLowerCase();
            if (text.includes(query)) {
                child.style.display = 'block';
                hasVisible = true;
            } else {
                child.style.display = 'none';
            }
        });
        
        let noResultsInfo = this.optionsList.querySelector('.no-results');
        if (!hasVisible) {
            if (!noResultsInfo) {
                noResultsInfo = document.createElement('div');
                noResultsInfo.className = 'custom-option no-results';
                noResultsInfo.textContent = 'No results found';
                this.optionsList.appendChild(noResultsInfo);
            }
            noResultsInfo.style.display = 'block';
        } else if (noResultsInfo) {
            noResultsInfo.style.display = 'none';
        }
    }
}
