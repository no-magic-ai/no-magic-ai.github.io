/* Loader for the same-origin content snapshot (data/content.json, built by
   scripts/build_content.py). The snapshot is checked against the shape the
   builder documents before any page renders it, and pages build DOM through
   el()/externalLink() so repository text never becomes markup. */
'use strict';

(function () {
  const ORG = 'no-magic-ai';
  const OID = /^[0-9a-f]{40}$/;
  const SHA256 = /^[0-9a-f]{64}$/;
  const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
  const SCRIPT = /^[a-z0-9_]+$/;
  const RELEASE = /^v[0-9]+\.[0-9]+\.[0-9]+$/;
  const YEAR = /^[0-9]{4}$/;
  const DATE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;

  const TIERS = {
    '01-foundations': { label: 'Foundations', slug: 'foundations' },
    '02-alignment': { label: 'Alignment', slug: 'alignment' },
    '03-systems': { label: 'Systems', slug: 'systems' },
    '04-agents': { label: 'Agents', slug: 'agents' },
  };
  // Labels and one-line meanings follow no-magic's scripts/generate_catalog.py.
  const KINDS = {
    train_infer: { label: 'Train + infer', detail: 'learns from data, then uses what it learned' },
    comparison: { label: 'Comparison', detail: 'trains alternative arms and compares them' },
    forward_pass: { label: 'Forward pass', detail: 'runs an untrained mechanism; no learning phase' },
    algorithm_demo: { label: 'Algorithm demo', detail: 'runs a non-learning algorithm; no model is trained' },
  };
  const DATA_SOURCES = {
    in_script: { label: 'Data in script', detail: 'generated or written inline; no network' },
    names_download: { label: 'Downloads names.txt', detail: 'fetched on first run; needs network unless cached' },
  };
  const THEMES = [
    'efficient-inference', 'long-context', 'alignment', 'reasoning', 'architecture',
    'training-dynamics', 'parameter-efficient', 'interpretability', 'retrieval',
    'safety-robustness', 'agents', 'multimodal',
  ];
  const PAPER_STATUSES = [
    'triaged', 'summarized', 'backlog-implement', 'implemented', 'deprecated',
    'replaced', 'archived', 'reference-only',
  ];
  const LESSON_STATUSES = ['none', 'planned', 'drafted', 'published'];
  const ROUTING_KEYS = [
    'batch_label', 'decision', 'review_date', 'target_path', 'target_repo',
    'target_script_slug', 'target_tier',
  ];

  class ContentError extends Error {}

  function fail(message) { throw new ContentError(message); }
  function isObject(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }

  function object(value, where, keys) {
    if (!isObject(value)) fail(where + ' must be an object');
    if (keys) {
      const found = Object.keys(value).sort().join(',');
      if (found !== keys.slice().sort().join(',')) fail(where + ' has keys ' + found);
    }
    return value;
  }
  function string(value, where, nullable) {
    if (value === null && nullable) return null;
    if (typeof value !== 'string' || value === '') fail(where + ' must be a non-empty string');
    return value;
  }
  function list(value, where) {
    if (!Array.isArray(value)) fail(where + ' must be a list');
    return value;
  }
  function strings(value, where) {
    list(value, where).forEach(function (item, i) { string(item, where + '[' + i + ']'); });
    return value;
  }
  function oneOf(value, allowed, where) {
    string(value, where);
    if (!allowed.includes(value)) fail(where + ' has unknown value ' + JSON.stringify(value));
    return value;
  }
  function matching(value, pattern, where, nullable) {
    if (value === null && nullable) return null;
    string(value, where);
    if (!pattern.test(value)) fail(where + ' is malformed: ' + JSON.stringify(value));
    return value;
  }
  function exactly(value, expected, where) {
    if (value !== expected) fail(where + ' is not the cohort-pinned URL ' + expected);
    return value;
  }
  function httpUrl(value, where) {
    string(value, where);
    let url;
    try { url = new URL(value); } catch (err) { fail(where + ' is not an absolute URL'); }
    if ((url.protocol !== 'http:' && url.protocol !== 'https:') || !url.hostname ||
        url.username || url.password || /\s/.test(value)) {
      fail(where + ' must be an http(s) URL without credentials');
    }
    return value;
  }

  function blobUrl(repo, commit, path) {
    return 'https://github.com/' + ORG + '/' + repo + '/blob/' + commit + '/' + path;
  }
  function rawUrl(repo, commit, path) {
    return 'https://raw.githubusercontent.com/' + ORG + '/' + repo + '/' + commit + '/' + path;
  }

  function checkCohort(value) {
    const cohort = object(value, 'cohort', ['inputs', 'receipt_sha256', 'repositories']);
    matching(cohort.receipt_sha256, SHA256, 'cohort.receipt_sha256');
    const repos = object(cohort.repositories, 'cohort.repositories', ['no-magic', 'no-magic-papers', 'no-magic-viz']);
    Object.keys(repos).forEach(function (name) {
      const repo = object(repos[name], 'cohort.repositories.' + name, ['commit', 'tree']);
      matching(repo.commit, OID, name + '.commit');
      matching(repo.tree, OID, name + '.tree');
    });
    return {
      core: repos['no-magic'].commit,
      papers: repos['no-magic-papers'].commit,
      viz: repos['no-magic-viz'].commit,
    };
  }

  function checkAlgorithm(a, i, commits) {
    const where = 'algorithms[' + i + ']';
    object(a, where, [
      'adaptation_note', 'data_source', 'declared_commit', 'declared_release', 'display', 'lines',
      'media', 'name', 'paper_slug', 'source_path', 'source_url', 'teaching_kind', 'thesis', 'tier',
    ]);
    matching(a.name, SCRIPT, where + '.name');
    oneOf(a.tier, Object.keys(TIERS), where + '.tier');
    string(a.display, where + '.display');
    string(a.thesis, where + '.thesis');
    if (!Number.isInteger(a.lines) || a.lines <= 0) fail(where + '.lines must be a positive integer');
    matching(a.paper_slug, SLUG, where + '.paper_slug');
    oneOf(a.teaching_kind, Object.keys(KINDS), where + '.teaching_kind');
    oneOf(a.data_source, Object.keys(DATA_SOURCES), where + '.data_source');
    string(a.adaptation_note, where + '.adaptation_note', true);
    if (a.source_path !== a.tier + '/' + a.name + '.py') fail(where + '.source_path is not tier/name.py');
    exactly(a.source_url, blobUrl('no-magic', commits.core, a.source_path), where + '.source_url');
    matching(a.declared_commit, OID, where + '.declared_commit', true);
    matching(a.declared_release, RELEASE, where + '.declared_release', true);
    const media = object(a.media, where + '.media', ['note', 'preview_url', 'scene_url', 'status']);
    string(media.note, where + '.media.note', media.status === 'linked');
    if (media.status === 'linked') {
      exactly(media.preview_url, rawUrl('no-magic-viz', commits.viz, 'previews/' + a.name + '.gif'), where + '.media.preview_url');
      exactly(media.scene_url, blobUrl('no-magic-viz', commits.viz, 'scenes/scene_' + a.name + '.py'), where + '.media.scene_url');
    } else if (media.status === 'omitted') {
      if (media.preview_url !== null || media.scene_url !== null) fail(where + ' omitted media must not link files');
    } else {
      fail(where + '.media.status has unknown value ' + JSON.stringify(media.status));
    }
  }

  function checkPaper(p, i, commits) {
    const where = 'papers[' + i + ']';
    object(p, where, [
      'arxiv_id', 'authors', 'card_path', 'card_url', 'dependencies', 'discovered_date',
      'discovered_via', 'doi', 'implementations', 'lesson', 'routing', 'slug', 'status', 'tags',
      'themes', 'title', 'url', 'venue', 'year',
    ]);
    matching(p.slug, SLUG, where + '.slug');
    string(p.title, where + '.title');
    if (strings(p.authors, where + '.authors').length === 0) fail(where + '.authors is empty');
    string(p.venue, where + '.venue');
    matching(p.year, YEAR, where + '.year');
    string(p.arxiv_id, where + '.arxiv_id', true);
    string(p.doi, where + '.doi', true);
    httpUrl(p.url, where + '.url');
    string(p.discovered_via, where + '.discovered_via');
    matching(p.discovered_date, DATE, where + '.discovered_date');
    oneOf(p.status, PAPER_STATUSES, where + '.status');
    const themes = object(p.themes, where + '.themes', ['primary', 'secondary']);
    oneOf(themes.primary, THEMES, where + '.themes.primary');
    strings(themes.secondary, where + '.themes.secondary').forEach(function (t) { oneOf(t, THEMES, where + '.themes.secondary'); });
    strings(p.tags, where + '.tags');
    const routing = object(p.routing, where + '.routing', ROUTING_KEYS);
    ROUTING_KEYS.forEach(function (key) { string(routing[key], where + '.routing.' + key, true); });
    if (p.card_path !== 'papers/' + p.slug + '.md') fail(where + '.card_path is not papers/<slug>.md');
    exactly(p.card_url, blobUrl('no-magic-papers', commits.papers, p.card_path), where + '.card_url');
    strings(p.dependencies, where + '.dependencies').forEach(function (d) { matching(d, SLUG, where + '.dependencies'); });
    strings(p.implementations, where + '.implementations').forEach(function (n) { matching(n, SCRIPT, where + '.implementations'); });
    const lesson = object(p.lesson, where + '.lesson', ['path', 'status', 'url']);
    if (lesson.status !== null) oneOf(lesson.status, LESSON_STATUSES, where + '.lesson.status');
    string(lesson.path, where + '.lesson.path', true);
    if (lesson.status === 'drafted' || lesson.status === 'published') {
      exactly(lesson.url, blobUrl('no-magic-papers', commits.papers, 'lessons/' + p.slug + '.md'), where + '.lesson.url');
    } else if (lesson.url !== null) {
      fail(where + '.lesson.url is set for a ' + lesson.status + ' lesson');
    }
  }

  function validate(doc) {
    object(doc, 'snapshot', ['algorithms', 'cohort', 'papers', 'schema_version']);
    if (doc.schema_version !== 1) fail('snapshot schema_version must be 1');
    const commits = checkCohort(doc.cohort);
    list(doc.algorithms, 'algorithms').forEach(function (a, i) { checkAlgorithm(a, i, commits); });
    list(doc.papers, 'papers').forEach(function (p, i) { checkPaper(p, i, commits); });
    const byName = new Map(doc.algorithms.map(function (a) { return [a.name, a]; }));
    const bySlug = new Map(doc.papers.map(function (p) { return [p.slug, p]; }));
    if (byName.size !== doc.algorithms.length) fail('algorithms repeat a name');
    if (bySlug.size !== doc.papers.length) fail('papers repeat a slug');
    doc.algorithms.forEach(function (a) {
      const paper = bySlug.get(a.paper_slug);
      if (!paper || !paper.implementations.includes(a.name)) fail(a.name + ' is not listed by paper ' + a.paper_slug);
    });
    doc.papers.forEach(function (p) {
      p.implementations.forEach(function (n) {
        const a = byName.get(n);
        if (!a || a.paper_slug !== p.slug) fail(p.slug + ' lists ' + n + ', which does not name it');
      });
      p.dependencies.forEach(function (d) { if (!bySlug.has(d)) fail(p.slug + ' depends on unknown ' + d); });
    });
    return { cohort: doc.cohort, commits: commits, algorithms: doc.algorithms, papers: doc.papers, byName: byName, bySlug: bySlug };
  }

  async function load(url) {
    let response;
    try {
      response = await fetch(url, { cache: 'no-cache' });
    } catch (err) {
      throw new ContentError('could not fetch ' + url + ' (' + err.message + ')');
    }
    if (!response.ok) throw new ContentError('fetching ' + url + ' returned HTTP ' + response.status);
    let doc;
    try {
      doc = await response.json();
    } catch (err) {
      throw new ContentError(url + ' is not valid JSON');
    }
    return validate(doc);
  }

  // Build an element. `text` sets textContent; other props become attributes;
  // children may be nodes or strings (appended as text, never parsed as HTML).
  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.entries(props || {}).forEach(function (entry) {
      const key = entry[0];
      const value = entry[1];
      if (key === 'text') node.textContent = value;
      else if (value === true) node.setAttribute(key, '');
      else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, String(value));
    });
    (children || []).forEach(function (child) { if (child !== null) node.append(child); });
    return node;
  }

  // An outbound link that names the external host it depends on.
  function externalLink(href, label, className) {
    const host = new URL(href).hostname;
    return el('a', { href: href, class: className || 'ext-link', target: '_blank', rel: 'noopener noreferrer' }, [
      label,
      el('span', { class: 'ext-host', text: ' ↗ ' + host }),
    ]);
  }

  function shortCommit(commit) { return commit.slice(0, 7); }

  window.NoMagicContent = {
    load: load,
    el: el,
    externalLink: externalLink,
    shortCommit: shortCommit,
    ContentError: ContentError,
    TIERS: TIERS,
    KINDS: KINDS,
    DATA_SOURCES: DATA_SOURCES,
    THEMES: THEMES,
    PAPER_STATUSES: PAPER_STATUSES,
    LESSON_STATUSES: LESSON_STATUSES,
  };
})();
