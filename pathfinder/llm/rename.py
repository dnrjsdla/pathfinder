"""V21: some Ollama models drop tool parameters named `properties` or `description` (ollama/ollama#18468).
The model is shown `<name>_` instead; its arguments are mapped back, recorded names stay the originals."""
import copy

RENAMED = ("properties", "description")  # parameter names the Ollama tool-call path drops (V21, ollama#18468)


def _schema_out(schema, path, found):
    """`schema` with every key of a `properties` map that is in RENAMED suffixed `_` (more `_` while it
    collides with a sibling); `found[path] = {original: renamed}`, path = the original property names above."""
    if not isinstance(schema, dict):
        return schema
    schema = dict(schema)
    props = schema.get("properties")
    if isinstance(props, dict):
        names = {}
        for k in props:
            if k in RENAMED:
                names[k] = k + "_"
                while names[k] in props:
                    names[k] += "_"
        if names:
            found[path] = names
        schema["properties"] = {names.get(k, k): _schema_out(v, path + (k,), found) for k, v in props.items()}
        if isinstance(schema.get("required"), list):
            schema["required"] = [names.get(k, k) for k in schema["required"]]
    if "items" in schema:
        schema["items"] = _schema_out(schema["items"], path, found)
    return schema


def rename_out(tools):
    """(tools with RENAMED parameters renamed, renames = {tool: {path: {original: renamed}}})."""
    out, renames = [], {}
    for t in tools:
        found = {}
        out.append({**t, "parameters": _schema_out(t["parameters"], (), found)})
        if found:
            renames[t["name"]] = found
    return out, renames


def _at(value, path):
    """The dicts `path` leads to below `value` (lists are walked through)."""
    for v in value if isinstance(value, list) else [value]:
        if isinstance(v, list):
            yield from _at(v, path)
        elif isinstance(v, dict):
            if not path:
                yield v
            elif path[0] in v:
                yield from _at(v[path[0]], path[1:])


def _swap(tool_name, args, renames, back):
    args = copy.deepcopy(args)
    items = list(renames.get(tool_name, {}).items())  # parents before children
    for path, names in items if back else reversed(items):  # the path holds original names: outer ones change last on the way out
        for d in _at(args, path):
            for original, renamed in names.items():
                src, dst = (renamed, original) if back else (original, renamed)
                if src in d:
                    d[dst] = d.pop(src)  # both names present: the renamed one wins
    return args


def rename_in(tool_name, args, renames):
    """Arguments the model sent -> the tool's own names. An original-only key is kept (M9)."""
    return _swap(tool_name, args, renames, True)


def rename_args_out(tool_name, args, renames):
    """Recorded (original-name) arguments -> the names the model was shown, for replayed tool calls."""
    return _swap(tool_name, args, renames, False)
