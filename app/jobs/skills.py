"""Keyword skill matching between a job description and a CV. No AI, no network, no cost.

1. `job_skills(title, description)` finds known skills in a job ad and splits them into
   required and optional ("nice to have", "mile widziane", "bonus points", ...).
2. `cv_skills(content)` finds the same skills in a CV (skills list, experience, projects, ...).
3. `fit(job, cv)` compares them and returns a score, a label and the matched/missing skills.

Spoken languages are kept apart from technical skills: almost every ad mentions English
somewhere, so counting it as a skill would make every score look better than it is.
"""
import re
from typing import Any

# Bump when the dictionary or rules change, so stored job skills are recalculated.
SKILLS_VERSION = 4  # 4: skills joined by "or" are stored as alternatives

# (display name, regex). Matching is case-insensitive unless the name is in CASE_SENSITIVE.
_SKILLS: list[tuple[str, str]] = [
    # Programming languages
    ('Python', r'python'), ('Java', r'java(?!\s*script)'), ('JavaScript', r'javascript|js(?=\W)(?!on)|ecmascript'),
    ('TypeScript', r'typescript'), ('C#', r'c#|c\s*sharp'), ('C++', r'c\+\+|cpp'),
    ('C', r'C(?=\s*/\s*C\+\+)|C language|ANSI C|embedded C'),
    ('Go', r'golang|Go(?= (developer|engineer|language|programming))'), ('Rust', r'rust'), ('Kotlin', r'kotlin'),
    ('Swift', r'swift(?!ly)|swiftui'), ('PHP', r'php'), ('Ruby', r'ruby'), ('Scala', r'scala'),
    ('R', r'R(?= (language|programming|studio))|RStudio'),
    ('SQL', r'sql(?!ite)'), ('Bash', r'bash|shell scripting'), ('PowerShell', r'powershell'), ('MATLAB', r'matlab'),
    ('VBA', r'vba'), ('Dart', r'dart(?!s)'), ('Elixir', r'elixir'), ('Lua', r'lua'), ('Perl', r'perl'), ('Haskell', r'haskell'),
    # Web and mobile
    ('HTML', r'html5?'), ('CSS', r'css3?'), ('React', r'React(\.js|JS)?|react\.js|reactjs'), ('Angular', r'angular(js)?'),
    ('Vue.js', r'vue(\.js|js)?'), ('Next.js', r'next\.js|nextjs'), ('Node.js', r'node(\.js|js)|Node(?= backend)'),
    ('Svelte', r'svelte'), ('Tailwind', r'tailwind'), ('Redux', r'redux'), ('jQuery', r'jquery'),
    ('Android', r'android'), ('iOS', r'iOS'), ('Flutter', r'flutter'), ('React Native', r'react native'),
    ('Jetpack Compose', r'jetpack compose'), ('Express', r'express(\.js|js)'),
    # Backend frameworks
    ('Django', r'django'), ('Flask', r'flask'), ('FastAPI', r'fastapi'), ('Spring', r'spring( boot| framework| cloud|boot)'),
    ('.NET', r'\.net( core| framework)?|dotnet|asp\.net'), ('Laravel', r'laravel'), ('Symfony', r'symfony'),
    ('Ruby on Rails', r'rails|ruby on rails'), ('Hibernate', r'hibernate'), ('NestJS', r'nest\.?js'),
    # Only "REST" in capitals, "RESTful" or "REST API": plain "rest" is usually the English word.
    ('REST APIs', r'REST(ful)?( ?APIs?| services)?|[Rr]est (API|api)s?'), ('GraphQL', r'graphql'), ('gRPC', r'grpc'),
    ('Microservices', r'micro-?services'),
    # Databases
    ('PostgreSQL', r'postgres(ql)?'), ('MySQL', r'mysql|mariadb'), ('MongoDB', r'mongo(db)?'), ('Redis', r'redis'),
    ('SQL Server', r'sql server|mssql|t-sql'), ('Oracle Database', r'oracle (database|db)|pl/sql'), ('SQLite', r'sqlite'),
    ('Elasticsearch', r'elastic\s?search'), ('Cassandra', r'cassandra'), ('DynamoDB', r'dynamodb'), ('Snowflake', r'snowflake'),
    ('BigQuery', r'bigquery'), ('ClickHouse', r'clickhouse'),
    # Cloud and DevOps
    ('AWS', r'aws|amazon web services'), ('Azure', r'azure'), ('Google Cloud', r'gcp|google cloud'),
    ('Docker', r'docker'), ('Kubernetes', r'kubernetes|k8s'), ('Terraform', r'terraform'), ('Ansible', r'ansible'),
    ('CI/CD', r'ci\s*/\s*cd|continuous (integration|delivery|deployment)'), ('Jenkins', r'jenkins'),
    ('GitHub Actions', r'github actions'), ('GitLab CI', r'gitlab ci'),
    ('Git', r'git'),  # only the word "Git": "GitHub stars" in a company intro is not a requirement
    ('Linux', r'linux|unix|ubuntu'), ('Networking', r'tcp/ip|computer networks?'), ('Prometheus', r'prometheus'),
    ('Grafana', r'grafana'), ('Kafka', r'kafka'), ('RabbitMQ', r'rabbitmq'), ('Nginx', r'nginx'),
    # Data and AI
    ('pandas', r'pandas'), ('NumPy', r'numpy'), ('scikit-learn', r'scikit-?learn|sklearn'), ('TensorFlow', r'tensorflow'),
    ('PyTorch', r'pytorch'), ('Machine learning', r'machine learning|ML'), ('Deep learning', r'deep learning'),
    ('NLP', r'nlp|natural language processing'), ('Computer vision', r'computer vision'), ('LLMs', r'llms?|large language models?'),
    ('Spark', r'apache spark|pyspark|spark(?= (sql|streaming|jobs?))'), ('Airflow', r'airflow'), ('dbt', r'dbt'), ('ETL', r'etl|elt'),
    ('Data analysis', r'data analysis|data analytics'), ('Statistics', r'statistics|statistical'),
    ('Power BI', r'power\s?bi'), ('Tableau', r'tableau'), ('Looker', r'looker'), ('Excel', r'excel'),
    ('Data visualisation', r'data visuali[sz]ation'), ('Jupyter', r'jupyter'),
    # Testing and quality
    ('pytest', r'pytest'), ('JUnit', r'junit'), ('Selenium', r'selenium'), ('Cypress', r'cypress'), ('Playwright', r'playwright'),
    ('Jest', r'jest'), ('Unit testing', r'unit tests?|unit testing'), ('Test automation', r'test automation|automated test(s|ing)'),
    ('Manual testing', r'manual test(s|ing)'), ('Postman', r'postman'),
    # Ways of working and tools
    ('Agile', r'agile'), ('Scrum', r'scrum'), ('Kanban', r'kanban'), ('Jira', r'jira'), ('Confluence', r'confluence'),
    ('ServiceNow', r'servicenow'), ('Low-code', r'low-code|no-code'),
    # Design and media
    ('Figma', r'figma'), ('UX design', r'UX|user experience design|ux design'), ('UI design', r'ui design|user interface design'),
    ('Photoshop', r'photoshop'), ('Illustrator', r'illustrator'), ('Blender', r'blender'), ('Maya', r'Maya'),
    ('Unity', r'Unity(?= (engine|3d|developer))|Unity3D'), ('Unreal Engine', r'unreal( engine)?'),
    ('Motion capture', r'motion capture|mocap'), ('MotionBuilder', r'motionbuilder'), ('Video editing', r'video editing|premiere pro|after effects'),
    # Business and office
    ('SAP', r'SAP(?!\'s)'), ('Salesforce', r'salesforce'), ('CRM', r'crm'), ('Google Analytics', r'google analytics'),
    ('SEO', r'seo'), ('PowerPoint', r'powerpoint'), ('Accounting', r'accounting|księgowoś\w*'),
    ('Financial analysis', r'financial analysis|fp&a|financial modell?ing'), ('Project management', r'project management'),
    ('Customer support', r'customer (support|service)'), ('Sales', r'B2B sales|sales experience'),
    # Engineering and hardware
    ('PLC', r'plc'), ('AutoCAD', r'autocad'), ('SolidWorks', r'solidworks'), ('CATIA', r'catia'), ('Simulink', r'simulink'),
    ('Embedded systems', r'embedded( systems| software)'), ('Arduino', r'arduino'), ('Raspberry Pi', r'raspberry pi'),
    ('FPGA', r'fpga'), ('VHDL', r'vhdl'), ('Verilog', r'verilog'), ('ROS', r'ROS2?'),
    ('Cybersecurity', r'cyber\s?security|penetration testing|siem|soc analyst'),
]
CASE_SENSITIVE = {'C', 'Go', 'R', 'React', 'iOS', 'Node.js', 'Maya', 'Unity', 'SAP', 'ROS', 'REST APIs', 'UX design'}

_LANGUAGES: list[tuple[str, str]] = [
    ('English', r'english|angielsk\w*'), ('German', r'german|niemieck\w*|deutsch'), ('French', r'french|francusk\w*'),
    ('Spanish', r'spanish|hiszpańsk\w*'), ('Italian', r'italian|włosk\w*'), ('Polish', r'polish|polsk\w*'),
    ('Ukrainian', r'ukrainian|ukraińsk\w*'), ('Russian', r'russian|rosyjsk\w*'), ('Dutch', r'dutch|niderlandzk\w*'),
    ('Czech', r'czech|czesk\w*'), ('Portuguese', r'portuguese|portugalsk\w*'), ('Japanese', r'japanese|japońsk\w*'),
    ('Chinese', r'chinese|mandarin|chińsk\w*'),
]


def _combine(items: list[tuple[str, str]], flags: int) -> tuple[re.Pattern, dict[str, str]]:
    """One regex for many skills, so each text is scanned once (fast enough for every refresh)."""
    names = {f'g{i}': name for i, (name, _) in enumerate(items)}
    alternatives = '|'.join(f'(?P<g{i}>{pattern})' for i, (_, pattern) in enumerate(items))
    return re.compile(rf'(?<![\w.+#-])(?:{alternatives})(?![\w+#])', flags), names


_SENSITIVE = _combine([x for x in _SKILLS if x[0] in CASE_SENSITIVE], 0)
_INSENSITIVE = _combine([x for x in _SKILLS if x[0] not in CASE_SENSITIVE], re.I)
LANGUAGE_PATTERNS = [(name, re.compile(rf'\b(?:{p})\b', re.I)) for name, p in _LANGUAGES]

# A spoken language only counts as required when the ad asks for it, e.g. "fluent German",
# "English (C1)", "znajomość języka niemieckiego". "We offer English lessons" does not count.
_LANG_REQUIREMENT = re.compile(
    r'(fluent|fluency|proficien\w*|good command|communicative|business|native|written and spoken|spoken and written|'
    r'\b[ABC][12]\b|level|knowledge of|znajomoś\w*|biegł\w*|komunikatywn\w*|język\w*)', re.I)
_LANG_NOT_REQUIRED = re.compile(r'(lessons|classes|course|lekcje|kurs)', re.I)

# Section headers (short lines). Text after an "optional" header, until the next header, is optional.
_OPTIONAL_HEADER = re.compile(
    r'(nice[ -]to[ -]have|bonus points|would be a plus|is a plus|are a plus|preferred qualifications|'
    r'mile widzian\w*|dodatkowym atutem|good to have|amaze us|extra points|it.s a plus)', re.I)
_BENEFITS_HEADER = re.compile(r'(we offer|what we offer|benefits|oferujemy|what you.ll get|perks)', re.I)
# Company introductions mention the company's own products (Elasticsearch, AI, ...), not job requirements.
_INTRO_HEADER = re.compile(r'(about us|about the company|who we are|o nas|o firmie|our story|our mission)', re.I)
_REQUIRED_HEADER = re.compile(
    r'(requirements|wymagania|must[ -]have|what you.ll need|what we.re looking for|qualifications|responsibilities|'
    r'obowiązki|twoje zadania|your role|the role|about the role|role overview|o roli|what you.ll do|'
    r'what you will do|what you.ll be doing|your profile|twój profil|who you are|skills|looking for|'
    r'what will you (be )?do|what you will be doing|we expect|what we expect|your (main )?tasks|key responsibilities|'
    r'you will|you have|ideal candidate|oczekujemy|czego oczekujemy|wymagamy|zakres zadań|this position)', re.I)
_HEADERS = (('optional', _OPTIONAL_HEADER), ('benefits', _BENEFITS_HEADER), ('intro', _INTRO_HEADER),
            ('required', _REQUIRED_HEADER))


def find_skills(text: str) -> set[str]:
    found: set[str] = set()
    for pattern, names in (_SENSITIVE, _INSENSITIVE):
        for m in pattern.finditer(text or ''):
            found.add(names[m.lastgroup])
    return found


# Two skills joined only by "or" are alternatives: "FastAPI or Django" asks for one of them.
# "/" is not treated as "or", because "HTML/CSS" usually means both.
_OR = re.compile(r'\s*,?\s*(?:or|lub|albo)\s+', re.I)


def _alternatives(text: str) -> list[list[str]]:
    """Groups of skills joined by "or" on the same line, e.g. [['Django', 'FastAPI']]."""
    groups: list[list[str]] = []
    for line in (text or '').splitlines():
        hits = sorted((m.start(), m.end(), names[m.lastgroup])
                      for pattern, names in (_SENSITIVE, _INSENSITIVE) for m in pattern.finditer(line))
        run: list[str] = []
        prev_end = None
        for start, end, name in hits:
            if prev_end is not None and start < prev_end:
                continue  # overlapping match of the same words
            if run and _OR.fullmatch(line[prev_end:start]):
                run.append(name)
            else:
                if len(set(run)) > 1:
                    groups.append(sorted(set(run)))
                run = [name]
            prev_end = end
        if len(set(run)) > 1:
            groups.append(sorted(set(run)))
    return groups


def _header(line: str) -> str | None:
    stripped = line.strip(' -*:#\t•')
    if not stripped or len(stripped) >= 60:
        return None
    for name, pattern in _HEADERS:
        if pattern.search(stripped):
            return name
    return None


def _split_sections(description: str) -> tuple[str, str]:
    """Return (required_text, optional_text). Benefits and company intro are dropped."""
    lines = (description or '').splitlines()
    kinds = [_header(line) for line in lines]
    # With a requirements/role section, the text before the first header is the company intro.
    has_sections = any(k in ('required', 'optional') for k in kinds)
    mode = 'intro' if has_sections else 'required'
    required, optional, not_benefits = [], [], []
    for line, kind in zip(lines, kinds):
        mode = kind or mode
        if mode == 'required':
            required.append(line)
        elif mode == 'optional':
            optional.append(line)
        if mode != 'benefits':
            not_benefits.append(line)
    required_text = '\n'.join(required)
    if 'required' not in kinds:
        # The requirements section was not recognised: use the whole ad (without benefits) rather than nothing.
        required_text = '\n'.join(line for line in not_benefits if line not in optional)
    return required_text, '\n'.join(optional)


def _required_languages(text: str) -> set[str]:
    found = set()
    for name, pattern in LANGUAGE_PATTERNS:
        for m in pattern.finditer(text or ''):
            window = text[max(0, m.start() - 60): m.end() + 40]
            if _LANG_REQUIREMENT.search(window) and not _LANG_NOT_REQUIRED.search(window):
                found.add(name)
                break
    return found


def job_skills(title: str, description: str | None) -> dict[str, Any] | None:
    """Skills a job asks for, stored on each job posting. None when there is no description."""
    if not description:
        return None
    required_text, optional_text = _split_sections(description)
    required = find_skills(f'{title}\n{required_text}')
    optional = find_skills(optional_text) - required
    alternatives = {tuple(g) for g in _alternatives(required_text) + _alternatives(optional_text)}
    return {
        'required': sorted(required),
        'optional': sorted(optional),
        'alternatives': sorted(list(g) for g in alternatives),
        'languages': sorted(_required_languages(required_text)),
        'version': SKILLS_VERSION,
    }


def cv_text(content: dict[str, Any]) -> str:
    """All the text of a CV that can show a skill."""
    parts: list[str] = [content.get('summary') or '', ' , '.join(content.get('skills') or [])]
    for e in content.get('experience') or []:
        parts += [e.get('title') or '', ' '.join(e.get('bullets') or [])]
    for p in content.get('projects') or []:
        parts += [p.get('name') or '', p.get('tech') or '', p.get('description') or '']
    for ed in content.get('education') or []:
        parts += [ed.get('degree') or '', ed.get('note') or '']
    for c in content.get('certifications') or []:
        parts.append(c if isinstance(c, str) else ' '.join(str(v) for v in c.values()))
    return '\n'.join(parts)


def cv_skills(content: dict[str, Any]) -> dict[str, set[str]]:
    languages = {name for name, pattern in LANGUAGE_PATTERNS if pattern.search(content.get('languages') or '')}
    return {'skills': find_skills(cv_text(content)), 'languages': languages}


MIN_SKILLS = 3  # with fewer known skills in the ad, a score would mean nothing


def fit(job: dict[str, Any] | None, cv: dict[str, set[str]]) -> dict[str, Any]:
    """Compare stored job skills with CV skills. Optional skills count half."""
    empty = {'matched': [], 'missing': [], 'missingOptional': [], 'missingLanguages': []}
    if not job:
        return {'score': None, 'label': 'No description', **empty}
    required, optional = set(job['required']), set(job['optional'])
    have = cv['skills']
    missing_languages = sorted(set(job['languages']) - cv['languages'])
    # Each requirement is one unit: a single skill, or a group of alternatives ("FastAPI or Django")
    # that is met by any one of its skills. A group counts as required if any of its skills is.
    units: list[tuple[set[str], bool]] = []
    grouped: set[str] = set()
    for group in job.get('alternatives') or []:
        members = set(group) & (required | optional)
        if len(members) > 1 and not members & grouped:
            units.append((members, bool(members & required)))
            grouped |= members
    units += [({s}, True) for s in required - grouped] + [({s}, False) for s in optional - grouped]
    name = lambda members: ' or '.join(sorted(members))  # noqa: E731
    result = {
        'matched': sorted((required | optional) & have),
        'missing': sorted(name(m) for m, req in units if req and not m & have),
        'missingOptional': sorted(name(m) for m, req in units if not req and not m & have),
        'missingLanguages': missing_languages,
    }
    if len(required) + len(optional) < MIN_SKILLS:
        return {'score': None, 'label': 'Not enough detail', **result}
    total = sum(1 if req else 0.5 for _, req in units)
    earned = sum(1 if req else 0.5 for m, req in units if m & have)
    score = round(100 * earned / total)
    if missing_languages:  # a required language you do not speak is a real blocker
        score = min(score, 30)
    # Own wording on purpose: this keyword check must not look like the AI match verdict (Strong fit at 80+).
    label = ('High skill overlap' if score >= 60 else 'Some skill overlap' if score >= 35
             else 'Low skill overlap' if score > 0 else 'No skill overlap')
    return {'score': score, 'label': label, **result}
