"""Entity extraction from sessions."""

import re
from typing import List, Dict, Set, Tuple
from collections import Counter


# Known technical entities (expandable)
KNOWN_TECH = {
    'stella', 'triad', 'redis', 'firebase', 'postgresql', 'supabase',
    'react', 'typescript', 'python', 'fastapi', 'docker', 'kubernetes',
    'mongodb', 'sqlite', 'graphql', 'rest', 'api', 'websocket',
    'tailwind', 'css', 'html', 'javascript', 'node', 'npm',
    'git', 'github', 'ci', 'cd', 'pipeline', 'deployment',
    'testing', 'pytest', 'jest', 'cypress', 'playwright',
    'llm', 'openai', 'anthropic', 'claude', 'mistral',
    'embedding', 'vector', 'rag', 'prompt', 'token',
    'async', 'await', 'promise', 'callback', 'hook',
    'component', 'service', 'router', 'middleware', 'controller',
    'schema', 'migration', 'model', 'serializer', 'validator',
}

# File patterns
FILE_PATTERNS = [
    r'\b[\w\-]+\.(py|ts|tsx|js|jsx|json|yaml|yml|md|sql|sh|dockerfile)\b',
    r'\b[A-Z][a-zA-Z]+\.(py|ts|tsx)\b',  # PascalCase files (components)
]

# Bug/issue patterns
BUG_PATTERNS = [
    r'\b(race condition|memory leak|null pointer|segfault|deadlock)\b',
    r'\b(bug|issue|error|exception|crash|failure|broken)\b',
]

# Decision patterns — must be architectural, not conversational
DECISION_PATTERNS = [
    r'\b(decided to (use|adopt|switch|remove|replace|implement|refactor))\b',
    r'\b(decision (to|was|is):?\s)',
    r'\b(chose .* over .*)\b',
    r'\b(opted for)\b',
    r'\b(architectural decision)\b',
    r'\b(approach (is|was):?\s)',
]

# ── Quality filtering ──────────────────────────────────────────────────────

# IDE/tool metadata tags that leak into raw conversation text and should
# never appear in extracted patterns or decisions.
_NOISE_PATTERNS = [
    r'<ide_opened_file>.*?</ide_opened_file>',
    r'<ide_closed_file>.*?</ide_closed_file>',
    r'<system-reminder>.*?</system-reminder>',
    r'<additional_metadata>.*?</additional_metadata>',
    r'<user_actions>.*?</user_actions>',
    r'<memory_context>.*?</memory_context>',
    r'<rules[^>]*>.*?</rules>',
    r'<truncation_notice>.*?</truncation_notice>',
    r'<diff_block_start>.*?</diff_block_end>',
    r'<system_guidance>.*?</system_guidance>',
    r'<available_skills>.*?</available_skills>',
    r'<subagent_completion_notification[^>]*/>',
]

# Compiled noise stripper — removes XML-like metadata tags and their content
_NOISE_RE = re.compile('|'.join(_NOISE_PATTERNS), re.DOTALL | re.IGNORECASE)

# Secret patterns — extracted intelligence must never contain these.
_SECRET_PATTERNS = [
    r'sk-[a-zA-Z0-9]{20,}',           # OpenAI API keys
    r'snyk_[a-zA-Z0-9_.]{20,}',       # Snyk tokens
    r'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}',  # JWTs
    r'gh[pousr]_[a-zA-Z0-9]{20,}',    # GitHub tokens
    r'AKIA[A-Z0-9]{16}',              # AWS access keys
    r'xox[baprs]-[a-zA-Z0-9-]+',      # Slack tokens
    r'AIza[a-zA-Z0-9_-]{35}',         # Google API keys
    r'[a-f0-9]{40,}',                 # Hex secrets (SHA hashes, etc.)
]
_SECRET_RE = re.compile('|'.join(_SECRET_PATTERNS))

# Conversational phrases that indicate non-technical content — if a candidate
# pattern/decision starts with these, it's chat noise, not a code pattern.
_CONVERSATIONAL_STARTS = (
    "you are absolutely", "i sincerely apologize", "i apologize",
    "my billing", "thank you for", "thanks so much",
    "here is the console error", "those?", "things before",
    "it we have redeployed", "snyk here is the access",
)

# Technical indicators — a candidate pattern must contain at least one of
# these to qualify as a code pattern (not just conversation).
_TECH_INDICATORS = re.compile(
    r'(\.(py|ts|tsx|js|jsx|json|yaml|yml|sh|rb|go|rs)\b'
    r'|def \w+|class \w+|import \w+|from \w+'
    r'|function \w+|const \w+|let \w+|var \w+'
    r'|npm |pip |yarn |cargo |git '
    r'|error|exception|traceback|stack trace'
    r'|deploy|build|compile|lint|test'
    r'|api|endpoint|route|handler'
    r'|database|query|schema|migration'
    r'|component|hook|provider|service'
    r'|async|await|promise|callback'
    r'|redis|firebase|supabase|postgres|sqlite'
    r'|docker|kubernetes|nginx'
    r'|react|typescript|python|javascript)',
    re.IGNORECASE,
)


def _strip_noise(text: str) -> str:
    """Remove IDE/tool metadata tags and clean up whitespace."""
    cleaned = _NOISE_RE.sub('', text)
    # Collapse whitespace from removed tags
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    cleaned = re.sub(r' {2,}', ' ', cleaned)
    return cleaned.strip()


def _redact_secrets(text: str) -> str:
    """Replace detected secrets with [REDACTED]."""
    return _SECRET_RE.sub('[REDACTED]', text)


def _is_technical(text: str) -> bool:
    """Check if text contains technical indicators (not just conversation)."""
    return bool(_TECH_INDICATORS.search(text))


def _is_conversational(text: str) -> bool:
    """Check if text starts with conversational/chat phrases."""
    lower = text.lower().strip()
    return any(lower.startswith(p) for p in _CONVERSATIONAL_STARTS)


def _clean_pattern_description(desc: str) -> str:
    """Clean and validate a pattern description. Returns None if it fails quality checks."""
    desc = _strip_noise(desc)
    desc = _redact_secrets(desc)
    desc = desc.strip()
    # Remove leading/trailing punctuation artifacts
    desc = desc.strip('.,;:!? \n\t')
    # Quality gates
    if len(desc) < 20:
        return None
    if len(desc) > 300:
        desc = desc[:297] + "..."
    if _is_conversational(desc):
        return None
    if not _is_technical(desc):
        return None
    if _SECRET_RE.search(desc):
        return None  # double-check: reject if any secret survived redaction
    return desc


class EntityExtractor:
    """Extracts entities, decisions, and patterns from sessions."""
    
    def __init__(self):
        self.entities: Dict[str, Dict] = {}
    
    def extract_from_session(self, session: Dict) -> Dict:
        """Extract all intelligence from a session.
        
        Returns:
            {
                "entities": List[{"name": str, "type": str, "context": str}],
                "decisions": List[{"title": str, "context": str, "decision": str}],
                "patterns": List[{"type": str, "description": str}],
                "files": List[str],
                "tags": List[str]
            }
        """
        result = {
            "entities": [],
            "decisions": [],
            "patterns": [],
            "files": [],
            "tags": []
        }
        
        # Combine all text from turns, stripping IDE/tool metadata noise
        raw_text = " ".join(
            t.get("content", "") for t in session.get("turns", [])
        )
        all_text = _strip_noise(raw_text)
        all_text = _redact_secrets(all_text)
        
        # Extract entities
        result["entities"] = self._extract_entities(all_text)
        
        # Extract decisions
        result["decisions"] = self._extract_decisions(all_text, session)
        
        # Extract patterns
        result["patterns"] = self._extract_patterns(all_text, session)
        
        # Extract files
        result["files"] = self._extract_files(all_text, session)
        
        # Extract tags
        result["tags"] = self._extract_tags(all_text)
        
        return result
    
    def _extract_entities(self, text: str) -> List[Dict]:
        """Extract named entities from text."""
        entities = []
        text_lower = text.lower()
        
        # Known technical entities
        for tech in KNOWN_TECH:
            if tech in text_lower:
                # Find context
                idx = text_lower.find(tech)
                start = max(0, idx - 50)
                end = min(len(text), idx + len(tech) + 50)
                context = text[start:end]
                
                entities.append({
                    "name": tech,
                    "type": "technology",
                    "context": context
                })
        
        # Extract potential proper nouns (project names, people)
        # Pattern: Capitalized words in technical contexts
        proper_nouns = re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', text)
        for noun in set(proper_nouns):
            if len(noun) > 3 and noun.lower() not in KNOWN_TECH:
                entities.append({
                    "name": noun,
                    "type": "concept",
                    "context": ""
                })
        
        # File references
        file_refs = re.findall(r'`([^`]+\.(py|ts|tsx|js|jsx|md|json))`', text)
        for ref in file_refs:
            entities.append({
                "name": ref[0],
                "type": "file",
                "context": ""
            })
        
        return entities
    
    def _extract_decisions(self, text: str, session: Dict) -> List[Dict]:
        """Extract architectural decisions from text.

        Quality-filtered: only sentences with explicit decision language AND
        technical context are kept. Conversational "I decided to..." without
        technical indicators is rejected.
        """
        decisions = []
        seen_titles = set()  # Deduplicate within session
        sentences = re.split(r'[.!?]+', text)

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 30 or len(sentence) > 500:
                continue

            # Must match a decision pattern (architectural, not conversational)
            is_decision = any(
                re.search(pattern, sentence, re.IGNORECASE)
                for pattern in DECISION_PATTERNS
            )
            if not is_decision:
                continue

            # Must have technical context
            if not _is_technical(sentence):
                continue
            if _is_conversational(sentence):
                continue

            # Redact secrets
            sentence = _redact_secrets(sentence)

            title = sentence[:100] + ("..." if len(sentence) > 100 else "")
            # Deduplicate by title prefix
            title_key = title[:60].lower()
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)

            decisions.append({
                "title": title,
                "context": session.get("summary", ""),
                "decision": sentence
            })

        return decisions[:5]  # Limit to top 5
    
    def _extract_patterns(self, text: str, session: Dict) -> List[Dict]:
        """Extract code patterns and fixes.

        Quality-filtered: only patterns with technical context are kept.
        Conversational text, IDE metadata, secrets, and non-code content
        are rejected. Patterns are deduplicated within the session.
        """
        patterns = []
        seen_descs = set()  # Deduplicate within session

        # Bug fixes — find ALL fix contexts, not just the first
        if any(re.search(p, text, re.IGNORECASE) for p in BUG_PATTERNS):
            fix_matches = re.finditer(
                r'(?:fixed|fix|resolved|solved|patched)\s+(.{50,300})',
                text,
                re.IGNORECASE
            )
            for match in fix_matches:
                desc = _clean_pattern_description(match.group(1))
                if desc and desc[:60] not in seen_descs:
                    seen_descs.add(desc[:60])
                    patterns.append({"type": "fix", "description": desc})

        # Refactoring patterns
        refactor_matches = re.finditer(
            r'(?:refactor|restructure|reorganize|simplify)\s+(.{50,300})',
            text,
            re.IGNORECASE
        )
        for match in refactor_matches:
            desc = _clean_pattern_description(match.group(1))
            if desc and desc[:60] not in seen_descs:
                seen_descs.add(desc[:60])
                patterns.append({"type": "refactor", "description": desc})

        # Code examples in fenced code blocks — must be actual code, not
        # slash commands or markdown prose
        code_blocks = re.findall(r'```(\w*)\n(.*?)```', text, re.DOTALL)
        for lang, block in code_blocks[:5]:
            block = block.strip()
            if len(block) < 50:
                continue
            # Reject slash-command blocks (e.g. "/boris <task>")
            if block.startswith('/'):
                continue
            # Reject markdown/text blocks
            if lang.lower() in ('markdown', 'md', 'text', ''):
                # Only accept if it looks like code (has indentation, keywords)
                if not re.search(r'(def |class |function |const |import |from |if |for |return )', block):
                    continue
            desc = _redact_secrets(block[:200])
            if desc and desc[:60] not in seen_descs:
                seen_descs.add(desc[:60])
                patterns.append({
                    "type": "code_example",
                    "description": desc + ("..." if len(block) > 200 else "")
                })

        return patterns[:10]  # Hard cap per session
    
    def _extract_files(self, text: str, session: Dict) -> List[str]:
        """Extract file references."""
        files = set()
        
        # Inline code references
        inline_files = re.findall(r'`([^`]+\.(py|ts|tsx|js|jsx|md|json|yaml))`', text)
        for match in inline_files:
            files.add(match[0])
        
        # File path mentions
        path_files = re.findall(r'(?:file|path|in)\s+[`\']?(?:\./)?([\w/\-]+\.(?:py|ts|tsx|js|jsx|md))', text, re.IGNORECASE)
        files.update(path_files)
        
        # From git turns
        for turn in session.get("turns", []):
            if "files_changed" in turn:
                for f in turn["files_changed"]:
                    if isinstance(f, dict):
                        files.add(f.get("path", ""))
                    elif isinstance(f, str):
                        files.add(f)
        
        return sorted(f for f in files if f)
    
    def _extract_tags(self, text: str) -> List[str]:
        """Extract topic tags."""
        text_lower = text.lower()
        tags = Counter()
        
        # Technical areas
        tech_areas = {
            'frontend': ['react', 'component', 'ui', 'css', 'html', 'dom'],
            'backend': ['api', 'server', 'endpoint', 'route', 'database'],
            'devops': ['docker', 'deploy', 'pipeline', 'ci', 'cd', 'kubernetes'],
            'testing': ['test', 'pytest', 'jest', 'spec', 'coverage'],
            'database': ['sql', 'postgres', 'mongodb', 'migration', 'schema'],
            'ai': ['llm', 'prompt', 'embedding', 'model', 'agent', 'rag'],
            'security': ['auth', 'token', 'secret', 'sanitize', 'injection'],
            'performance': ['optimize', 'speed', 'cache', 'memory', 'slow'],
            'bugfix': ['bug', 'fix', 'error', 'crash', 'broken'],
            'feature': ['feat', 'add', 'implement', 'new', 'create'],
        }
        
        for area, keywords in tech_areas.items():
            score = sum(1 for kw in keywords if kw in text_lower)
            if score > 0:
                tags[area] = score
        
        # Return top tags
        return [tag for tag, _ in tags.most_common(5)]


def extract_entity_links(entities: List[Dict]) -> List[Tuple[str, str, float]]:
    """Extract co-occurrence links between entities.
    
    Returns list of (entity_a, entity_b, strength) tuples.
    """
    links = []
    entity_names = [e["name"] for e in entities]
    
    # Simple co-occurrence: entities mentioned in same session are linked
    for i, a in enumerate(entity_names):
        for b in entity_names[i+1:]:
            links.append((a, b, 1.0))
    
    return links
