// Home artwork follows the confirmed plan, never a quick-action click or a preference.
(() => {
  const activities = {
    meal: {
      label: '밥을 먹는 볼티',
      lines: ['든든하게 한 끼 먹고 와요!', '볼티도 같이 냠냠.'],
      caption: '배도 마음도 든든해지는 시간.',
    },
    cafe: {
      label: '커피를 마시는 볼티',
      lines: ['커피 한 모금, 여유 한 모금.', '볼티랑 잠깐 쉬어가요.'],
      caption: '따뜻한 한 잔으로 채우는 작은 여유.',
    },
    convenience: {
      label: '편의점 앞에서 기다리는 볼티',
      lines: ['가볍게 편의점에 다녀와요!', '작은 간식 하나면 기분도 충전.'],
      caption: '가까운 곳에서 찾는 소소한 즐거움.',
    },
    mart: {
      label: '장바구니 카트를 미는 볼티',
      lines: ['필요한 것만 쏙쏙 담아볼까요?', '볼티가 카트를 밀어줄게요.'],
      caption: '기다리는 틈에, 오늘의 장보기도 끝.',
    },
  };

  window.VoltGoHomeCharacter = class {
    constructor(button, speech, caption) {
      this.button = button;
      this.speech = speech;
      this.caption = caption;
      this.greeting = button.querySelector(':scope > svg');
      this.defaultSpeech = speech.innerHTML;
      this.defaultCaption = caption.textContent;
      this.defaultLabel = button.getAttribute('aria-label');
      this.defaultTitle = button.title;
      this.images = new Map();
      this.revision = 0;
      this.category = '';
      button.dataset.activity = 'hello';
      this.confirmation = null;
      this.stage = document.createElement('span');
      this.stage.className = 'volty-activity';
      this.stage.hidden = true;
      this.stage.setAttribute('aria-hidden', 'true');
      this.sprite = document.createElement('span');
      this.sprite.className = 'volty-activity-sprite';
      this.stage.append(this.sprite);
      button.append(this.stage);
      speech.setAttribute('aria-live', 'polite');
    }

    resolve(session, response) {
      const confirmed = session?.confirmed;
      if (!confirmed || (session.condition_version != null && confirmed.version !== session.condition_version)) {
        this.confirmation = null;
        return '';
      }
      const key = JSON.stringify([confirmed.plan_id, confirmed.version, confirmed.confirmed_at]);
      const candidates = [
        ...(Array.isArray(session.candidates) ? session.candidates : []),
        ...(response?.status === 'confirmed' && Array.isArray(response.candidates) ? response.candidates : []),
      ];
      const selected = candidates.find(plan => plan.plan_id === confirmed.plan_id && plan.version === confirmed.version);
      if (selected && !Object.hasOwn(activities, selected.category)) {
        this.confirmation = null;
        return '';
      }
      if (selected && Object.hasOwn(activities, selected.category)) {
        this.confirmation = {key, category: selected.category};
      }
      return this.confirmation?.key === key ? this.confirmation.category : '';
    }

    render(session = {}, response = null) {
      const category = this.resolve(session, response);
      if (category === this.category) return;
      this.category = category;
      const revision = ++this.revision;
      this.button.dataset.activity = category || 'hello';
      this.button.classList.remove('has-activity');
      this.greeting.removeAttribute('hidden');
      this.stage.hidden = true;
      if (!category) {
        this.speech.innerHTML = this.defaultSpeech;
        this.caption.textContent = this.defaultCaption;
        this.button.setAttribute('aria-label', this.defaultLabel);
        this.button.title = this.defaultTitle;
        return;
      }
      const activity = activities[category];
      this.speech.replaceChildren();
      activity.lines.forEach((line, index) => {
        if (index) this.speech.append(document.createElement('br'));
        this.speech.append(document.createTextNode(line));
      });
      this.caption.textContent = activity.caption;
      this.button.setAttribute('aria-label', activity.label + '. 눌러서 다시 보기');
      this.button.title = '볼티를 누르면 동작을 다시 보여줘요';
      const src = `img/volty-activities/${category}-smooth.png`;
      if (!this.images.has(src)) {
        const image = new Image();
        this.images.set(src, new Promise(resolve => {
          image.onload = () => resolve(true);
          image.onerror = () => { this.images.delete(src); resolve(false); };
          image.src = src;
        }));
      }
      this.images.get(src).then(loaded => {
        // A previous activity must not reappear after a new plan or conversation.
        if (revision !== this.revision) return;
        if (!loaded) {
          this.button.setAttribute('aria-label', this.defaultLabel);
          this.button.title = this.defaultTitle;
          return;
        }
        this.sprite.style.backgroundImage = `url("${src}")`;
        this.greeting.setAttribute('hidden', '');
        this.stage.hidden = false;
        this.button.classList.add('has-activity');
      });
    }
  };
})();
