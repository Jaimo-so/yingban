"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";

import styles from "./landing.module.css";

const asset = (name: string) => `/landing-assets/${name}`;
const clamp = (value: number, low = 0, high = 1) => Math.min(high, Math.max(low, value));

const scenes = [
  {
    eyebrow: "一个真正记得你电影经验的 AI 电影伙伴",
    title: ["下一部电影，", "从这里开始。"],
    description: "从看过的电影出发，找到此刻想看的下一部，也把看完后的感受慢慢说清楚。",
    action: "看看影伴能做什么",
    href: "#features",
  },
  {
    eyebrow: "看完之后",
    title: ["感受说出来，", "故事就有了后续。"],
    description: "从一句真实感受开始聊。不必先写成影评，也不必先想好标准答案。",
    action: "了解电影讨论",
    href: "#features",
  },
  {
    eyebrow: "属于你的电影轨迹",
    title: ["看过的、想看的，", "都由你决定。"],
    description: "重要的电影证据由你确认；记忆看得见，也能修改和删除。",
    action: "看看我的电影",
    href: "#boards",
  },
];

const features = [
  ["从感受开始聊", "刚看完，不必先组织观点。说一句真实感受，影伴会陪你继续聊。"],
  ["找下一部电影", "从口味与当下情境出发推荐，并排除已经看过的电影。"],
  ["留下观影轨迹", "把看过、想看和不感兴趣整理成属于自己的电影记录。"],
  ["记忆由你决定", "重要内容看得见、改得动，也能撤回和删除。"],
];

const showcase = [
  {
    eyebrow: "01 / 今晚",
    title: "找下一部电影",
    description: "从看过的电影和此刻的心情出发，找到一部想看的。",
    image: "story-discover.jpg",
    screenshot: "yingban-recommend.jpg",
    screenshotAlt: "影伴根据此刻心情寻找电影的界面示意",
    previewLabel: "影伴 · 找电影",
    previewTitle: "今晚想看一部温柔一点的电影。",
    previewDetail: "从口味出发 · 避开已看",
  },
  {
    eyebrow: "02 / 散场之后",
    title: "聊聊刚看完的电影",
    description: "从一句真实感受开始，不必先想好标准答案。",
    image: "story-discuss.jpg",
    screenshot: "yingban-chat.jpg",
    screenshotAlt: "影伴与用户讨论刚看完的电影的界面示意",
    previewLabel: "影伴 · 聊电影",
    previewTitle: "刚看完，心里还停在那一幕。",
    previewDetail: "从感受开始 · 慢慢说清楚",
  },
  {
    eyebrow: "03 / 留下来",
    title: "留下观后感笔记",
    description: "把聊出来的感受整理成草稿。由你编辑、确认，之后随时回看。",
    image: "story-remember.jpg",
  },
];

const boards = [
  { title: "看过", description: "每一部看过的电影，都能成为下一次理解你的证据。", images: ["yingban-my-movies.jpg", "yingban-chat.jpg", "yingban-recommend.jpg"] },
  { title: "想看", description: "把此刻心动的电影留在片单，想看的时候再回来。", images: ["yingban-recommend.jpg", "yingban-my-movies.jpg", "yingban-home.jpg"] },
  { title: "不感兴趣", description: "拒绝过的电影也有意义；影伴会尊重你的选择。", images: ["yingban-my-movies.jpg", "yingban-chat.jpg", "yingban-home.jpg"] },
];

const moments = [
  ["刚散场，心里有点乱。先说一句我喜欢，却说不上为什么。", "聊电影"],
  ["今晚想看一部电影，但不想再从热榜里盲选。", "找电影"],
  ["聊完刚看过的电影，把想留下的感受整理成观后感笔记。确认之后，随时可以回来读。", "观后感笔记"],
];

function Arrow({ size = 19 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M5 12h14m-6-6 6 6-6 6" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function FeatureIcon({ index }: { index: number }) {
  const paths = [
    <><path d="M12 20a8 8 0 1 0-8-8c0 1.5.4 2.8 1.1 3.9L4 20l4.1-1.1A8 8 0 0 0 12 20Z" /><path d="M8.5 11.5c1.3-1.2 2.7-1.2 4 0m-4 3c1.8-1.5 4-1.5 5.8 0" /></>,
    <><circle cx="10.8" cy="10.8" r="6.4" /><path d="m15.5 15.5 4.3 4.3M8.5 11l1.6 1.6 3.1-3.1" /></>,
    <><rect x="4" y="5" width="7" height="7" rx="1.4" /><rect x="13" y="5" width="7" height="7" rx="1.4" /><rect x="4" y="14" width="7" height="7" rx="1.4" /><path d="M13 14h7m-7 3.5h7M13 21h5" /></>,
    <><path d="M12 3c3.8 0 7 3.2 7 7v3c0 4.1-2.9 6.8-7 8-4.1-1.2-7-3.9-7-8v-3c0-3.8 3.2-7 7-7Z" /><path d="m9 12 2 2 4-4" /></>,
  ];
  return <svg viewBox="0 0 24 24" width="23" height="23" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[index]}</svg>;
}

function ShowcaseMedia({ index }: { index: number }) {
  const item = showcase[index];
  return (
    <div className={styles.showcaseMedia}>
      <img className={styles.showcasePhoto} src={asset(item.image)} alt="" loading="lazy" />
      <span className={styles.showcaseSceneLabel}>场景示意</span>
      {"screenshot" in item && item.screenshot ? (
        <>
          <div className={styles.showcaseScreenshot}><img src={asset(item.screenshot)} alt={item.screenshotAlt} loading="lazy" /></div>
          <div className={styles.showcaseCaption}>
            <span>{item.previewLabel}</span>
            <strong>{item.previewTitle}</strong>
            <small>{item.previewDetail}</small>
          </div>
        </>
      ) : (
        <div className={styles.showcaseReflection} aria-label="观后感笔记界面示意">
          <div className={styles.showcaseReflectionTop}><span>AFTER THE CREDITS</span><span>影伴 · 我的电影</span></div>
          <h4>观后感笔记</h4>
          <p className={styles.showcaseReflectionMovie}>一部电影，一份属于自己的感受</p>
          <div className={styles.showcaseReflectionRule} />
          <p className={styles.showcaseReflectionBody}>从聊天中整理的感受，先是一份可编辑的草稿。想留下哪些话，由你决定。</p>
          <div className={styles.showcaseReflectionBottom}><span>可编辑草稿</span><span>确认后保存</span></div>
        </div>
      )}
    </div>
  );
}

export default function LandingExperience() {
  const [booting, setBooting] = useState(true);
  const [activeBoard, setActiveBoard] = useState(0);
  const heroTrack = useRef<HTMLElement>(null);
  const heroMedia = useRef<HTMLDivElement>(null);
  const heroPanels = useRef<(HTMLElement | null)[]>([]);
  const showcaseTrack = useRef<HTMLDivElement>(null);
  const showcaseCards = useRef<(HTMLElement | null)[]>([]);
  const progressBar = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timer = window.setTimeout(() => setBooting(false), reduceMotion ? 0 : 720);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    const reveals = document.querySelectorAll(`.${styles.reveal}`);
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add(styles.inView);
          observer.unobserve(entry.target);
        }
      }
    }, { threshold: 0.12 });
    reveals.forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let frame = 0;
    const update = () => {
      frame = 0;
      const y = window.scrollY;
      const vh = window.innerHeight;
      const root = document.documentElement;
      const total = Math.max(root.scrollHeight - vh, 1);
      if (progressBar.current) progressBar.current.style.transform = `scaleX(${clamp(y / total)})`;

      const hero = heroTrack.current;
      if (hero) {
        const start = hero.getBoundingClientRect().top + y;
        const p = clamp((y - start) / Math.max(hero.offsetHeight - vh, 1));
        if (heroMedia.current && !reduceMotion) heroMedia.current.style.transform = `translate3d(${(p * 4 - 2).toFixed(2)}%, 0, 0) scale(${(1.04 + p * 0.12).toFixed(3)})`;
        heroPanels.current.forEach((panel, index) => {
          if (!panel) return;
          const distance = Math.abs(p * 2 - index);
          const opacity = reduceMotion ? (index === 0 ? 1 : 0) : clamp(1 - distance * 1.8);
          panel.style.opacity = String(opacity);
          panel.style.transform = `translate(-50%, calc(-50% + ${Math.round((1 - opacity) * 26)}px))`;
          panel.style.pointerEvents = opacity > 0.6 ? "auto" : "none";
          panel.inert = opacity <= 0.6;
          panel.setAttribute("aria-hidden", opacity > 0.6 ? "false" : "true");
        });
      }

      const showcase = showcaseTrack.current;
      if (showcase) {
        const stackCards = showcaseCards.current;
        if (reduceMotion || window.innerWidth < 1024) {
          stackCards.forEach((card) => {
            if (!card || !card.style.transform) return;
            card.style.removeProperty("transform");
            card.style.removeProperty("opacity");
          });
        } else {
          const start = showcase.getBoundingClientRect().top + y;
          const progress = clamp((y - start) / Math.max(vh, 1), 0, stackCards.length - 1);
          stackCards.forEach((card, index) => {
            if (!card) return;
            const entering = index === 0 ? 1 : clamp(progress - (index - 1));
            const exiting = clamp(progress - index);
            const offset = (index === 0 ? 0 : (1 - entering) * Math.min(vh * 0.95, 720)) + exiting * 72;
            const opacity = (index === 0 ? 1 : clamp(entering / 0.18)) * (1 - clamp(exiting * 1.25));
            card.style.transform = `translate3d(0, ${Math.round(offset)}px, 0) scale(${(1 - exiting * 0.035).toFixed(3)})`;
            card.style.opacity = String(opacity);
          });
        }
      }
    };
    const request = () => { if (!frame) frame = window.requestAnimationFrame(update); };
    update();
    window.addEventListener("scroll", request, { passive: true });
    window.addEventListener("resize", request);
    return () => {
      window.removeEventListener("scroll", request);
      window.removeEventListener("resize", request);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <div className={styles.page} id="top">
      {booting && <div className={styles.boot} role="status" aria-live="polite"><img className={styles.bootMark} src="/brand/yingban-seal-clean.svg" alt="" width="66" height="66" /><strong>影伴</strong><small>正在铺开今晚的银幕…</small></div>}
      <div className={styles.progress} ref={progressBar} aria-hidden="true" />
      <header className={styles.header}>
        <a className={styles.brand} href="#top" aria-label="影伴，回到页面顶部"><img src="/brand/yingban-seal-clean.svg" alt="" width="34" height="34" />影伴</a>
        <nav className={styles.nav} aria-label="页面导航"><a href="#features">能做什么</a><a href="#showcase">产品展示</a><a href="#boards">我的电影</a><a href="#moments">关于影伴</a></nav>
        <Link className={styles.navAction} href="/">打开影伴</Link>
      </header>

      <main>
        <section className={styles.heroTrack} ref={heroTrack} aria-label="影伴介绍">
          <div className={styles.heroStage}>
            <div className={styles.heroMedia} ref={heroMedia}><img src={asset("hero-screen.jpg")} alt="" /></div>
            <div className={styles.heroGrain} aria-hidden="true" />
            {scenes.map((scene, index) => (
              <article key={scene.eyebrow} className={styles.heroPanel} ref={(node) => { heroPanels.current[index] = node; }} style={{ opacity: index === 0 ? 1 : 0 }} aria-hidden={index !== 0} inert={index !== 0}>
                <p className={styles.heroEyebrow}>{scene.eyebrow}</p>
                <h1 className={styles.heroTitle}>{scene.title[0]}<br />{scene.title[1]}</h1>
                <p className={styles.heroDescription}>{scene.description}</p>
                <a className={styles.pill} href={scene.href}>{scene.action}<Arrow size={17} /></a>
              </article>
            ))}
            <span className={styles.heroIndex} aria-hidden="true">影像 · 记忆 · 下一部</span>
          </div>
        </section>

        <section className={styles.features} id="features" aria-labelledby="features-title">
          <div className={`${styles.sectionIntro} ${styles.reveal}`}><span className={styles.kicker}>影伴能做什么</span><h2 id="features-title">电影很多，真正稀缺的是被持续理解</h2><p>从真实看过的电影出发，让推荐、讨论和回顾彼此接得上。</p></div>
          <div className={styles.featureGrid}>{features.map(([title, description], index) => <article className={`${styles.featureCard} ${styles.reveal}`} style={{ transitionDelay: `${index * 90}ms` }} key={title}><span className={styles.featureIcon}><FeatureIcon index={index} /></span><h3>{title}</h3><p>{description}</p></article>)}</div>
          <div className={styles.featureTrail}>从一部电影开始 <span>→</span> 聊聊看完的感受 <span>→</span> 留下自己的轨迹</div>
        </section>

        <section className={styles.gallery} id="showcase" aria-labelledby="gallery-title">
          <div className={`${styles.galleryHead} ${styles.reveal}`}><div><span className={styles.kicker}>01 / 此刻与电影相处</span><h2 id="gallery-title">没想好怎么说。<br /><em>也可以先从感觉开始。</em></h2></div><p>想找一部电影、聊一部刚看完的电影，或者回头看看自己留下的轨迹。</p></div>
          <div className={styles.showcaseStack} ref={showcaseTrack}>
            <div className={styles.showcaseStage}>
              <div className={styles.showcaseTrack}>
                {showcase.map((item, index) => (
                  <article className={`${styles.showcaseCard} ${styles[`showcaseAccent${index + 1}`]}`} key={item.title} ref={(node) => { showcaseCards.current[index] = node; }} style={{ zIndex: index + 1, opacity: index === 0 ? 1 : 0 }}>
                    <header className={styles.showcaseCardHeader}><span className={styles.showcaseCardNumber}>0{index + 1}</span><div><span className={styles.showcaseCardEyebrow}>{item.eyebrow}</span><h3>{item.title}</h3><p>{item.description}</p></div></header>
                    <ShowcaseMedia index={index} />
                  </article>
                ))}
              </div>
            </div>
          </div>
          <div className={styles.showcaseAfter}><p>从一个感觉，走进更多可能。</p><Link href="/">打开影伴 <Arrow size={17} /></Link></div>
        </section>

        <section className={styles.boards} id="boards" aria-labelledby="boards-title">
          <div className={styles.boardsInner}>
            <div className={`${styles.boardsCopy} ${styles.reveal}`}><span className={styles.kicker}>02 / 我的电影</span><h2 id="boards-title">少一点遗忘。<br /><em>多一点属于自己。</em></h2><p>把看过的、想看的，和不想看的放在一起，下一次选择会更有来处。</p><div className={styles.boardTabs} aria-label="查看电影分类">{boards.map((board, index) => <button type="button" key={board.title} aria-pressed={activeBoard === index} onClick={() => setActiveBoard(index)}><small>0{index + 1}</small><span>{board.title}</span><Arrow size={18} /></button>)}</div><p className={styles.boardDescription} aria-live="polite">{boards[activeBoard].description}</p></div>
            <div className={`${styles.boardVisual} ${styles.reveal}`} aria-label={`${boards[activeBoard].title}界面预览`}><div className={styles.boardTop}><span>YOUR MOVIE JOURNEY</span><span>影伴 · 我的电影 ↗</span></div><div className={styles.boardShots} key={activeBoard}>{boards[activeBoard].images.map((image, index) => <figure className={`${styles.boardShot} ${styles[`boardShot${index + 1}`]}`} key={`${image}-${index}`}><img src={asset(image)} alt={`影伴界面截图 ${index + 1}`} /></figure>)}</div><div className={styles.boardBottom}><div><small>电影会继续，理解也会继续</small><strong>{boards[activeBoard].title}</strong></div><Link href="/" aria-label="打开影伴，查看我的电影">打开我的电影 <Arrow size={16} /></Link></div></div>
          </div>
        </section>

        <section className={styles.moments} id="moments" aria-labelledby="moments-title"><div className={`${styles.sectionIntro} ${styles.reveal}`}><span className={styles.kicker}>当电影成为自己的经历</span><h2 id="moments-title">你可能会遇到的三个时刻</h2><p>从当下的一句话开始，慢慢留下可以回看的东西。</p></div><div className={styles.momentGrid}>{moments.map(([text, label], index) => <article className={`${styles.momentCard} ${styles.reveal}`} key={label} style={{ transitionDelay: `${index * 90}ms` }}><span className={styles.quoteMark}>“</span><p>{text}</p><div><span className={styles.momentNumber}>0{index + 1}</span><strong>{label}</strong></div></article>)}</div><p className={styles.momentsNote}>以上为使用场景示例，不代表真实用户评价。</p></section>

        <section className={styles.finalCta} aria-labelledby="final-title"><div className={styles.reveal}><span className={styles.kicker}>银幕尚未暗下</span><h2 id="final-title">下一部电影，<br />从这里开始。</h2><p>影伴会记得你与电影的关系，而每一步仍由你决定。</p><Link className={styles.pill} href="/">打开影伴 <Arrow size={17} /></Link></div></section>
      </main>

      <footer className={styles.footer}><div className={styles.footerGrid}><div className={styles.footerBrand}><div><img src="/brand/yingban-seal-clean.svg" width="38" height="38" alt="" /><strong>影伴</strong></div><p>让推荐有来处，让讨论有后续，<br />让记忆始终由你决定。</p></div><div><h3>产品</h3><Link href="/">打开影伴</Link><a href="#features">能做什么</a><a href="#boards">我的电影</a></div><div><h3>了解影伴</h3><a href="#showcase">产品展示</a><a href="#moments">使用场景</a></div></div><div className={styles.footerBottom}><span>© 2026 影伴</span><a href="#top">回到顶部 ↑</a></div></footer>
    </div>
  );
}
