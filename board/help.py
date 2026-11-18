"""The single source of truth for `board help`, `board -h`, `board --help`, bare `board`."""

HELP = """\
board: post live updates to a web page that a human watches from a phone.
Use it to show what you are doing, what you found, what is failing, and
when you need the human.

WHO YOU ARE
  Every post and log needs -a/--agent NAME. Use a short role: lead, tests.
  Subagents use PARENT/NAME (lead/tests, lead/research) so the page
  groups them under their parent. Pass -a on every call: env vars do not
  persist between tool calls. (BOARD_AGENT is used when -a is absent.)

POST
  board post -a NAME TEXT            plain text (one or two sentences)
  board post -a NAME --md VALUE      Markdown
  board post -a NAME --table VALUE   JSON array of objects, JSON array of
                                     arrays (first row = header), or CSV
  board post -a NAME --chart VALUE   ECharts option object (see CHARTS)
  board post -a NAME --image FILE    PNG, JPEG, GIF, WebP or SVG

  VALUE is literal text. "-" reads stdin. "@path" reads a file.
  Text may start with "-" ("-3 tests failed"); text starting with "@" is
  posted as-is unless it names a file, so pipe it in with - if in doubt.
  Prints the new post ID (like p_3f9k2a) on stdout:
    id=$(board post -a lead "Starting the auth refactor")

  Options for every post:
    -t, --title TEXT   bold headline above the content
    -s, --status S     info (default), ok, warn, error, blocked
    -k, --key NAME     replace your earlier post with this key instead of
                       adding a new one (keeps the same ID). Use it for
                       anything you update: progress, scoreboards, charts.

STATUS (the human scans for these)
  ok       finished or passing       warn     odd but not stopping you
  error    something is failing      blocked  you need the human; say
                                              exactly what you need
  Your badge on the page is the status of your latest post.

LOGS (live, growing output)
  board log -a lead/tests -t "pytest" -- pytest -x
      Runs the command and streams its output live. Marks the post ok
      or error from the exit code and exits with that code.
  make build 2>&1 | board log -a lead -t "build"
      Streams stdin live. Stays info when done: set ok/error yourself.
      Programs may buffer piped output; prefer -- CMD (or python -u).
  Both print the ID first and copy the output to stderr, so you still
  see it. For long commands, run them in the background.
  board append ID TEXT     add text to a log, text or Markdown post
                           (TEXT can also be - or @path)
  board append ID -s ok    change a post's status (or -t its title);
                           do this once a blocked or error post is resolved

EXAMPLES
  board post -a lead "Read the codebase. Plan: fix auth, then tests."
  board post -a lead/research -t "Findings" --md - <<'EOF'
  ## Auth
  - Tokens never expire (`auth.py:88`)
  - **Fix:** add `exp` claim, 1h TTL
  EOF
  board post -a lead/research --md @notes/report.md
  board post -a lead/tests -t "Matrix" --table @results.csv
  board post -a lead/tests --table '[["test","result"],["login","pass"]]'
  python summarize.py | board post -a lead/tests --table -
  board post -a lead/ui -t "Login page after fix" --image shot.png
  board post -a lead -s blocked "Need the staging DB password to go on"
  board post -a lead -s ok -t "Done" "Auth fixed, 214/214 tests pass"

CHARTS
  --chart takes an Apache ECharts option object, so any chart type works:
  line, bar, scatter, pie, heatmap, candlestick, ... (reference:
  https://echarts.apache.org/en/option.html). Pure JSON only: no JS
  functions (template strings like "{b}: {c}" are fine). Theme, colors,
  tooltip, legend and zoom are added for you; set colors only when they
  mean something. For big data (thousands of points) put arrays in
  series[].data, e.g. [[x,y],...], or in dataset.source.

  board post -a lead/perf -k latency -t "p95 latency (ms)" --chart - <<'EOF'
  {"xAxis": {"type": "category", "data": ["v1", "v2", "v3", "v4"]},
   "yAxis": {"type": "value"},
   "series": [{"name": "api", "type": "line", "data": [120, 98, 87, 91]},
              {"name": "db", "type": "line", "data": [40, 42, 35, 30]}]}
  EOF
  Run it again with the same -k latency to update the chart in place.

MORE
  board ls [-a NAME] [-n 20]   recent posts: ID AGENT KIND STATUS TITLE
                               (-a lead also lists lead/* subagents)
  board serve [--port 8000] [--host 127.0.0.1]   start the web page
  Posting works even when the page server is not running; posts are
  saved in ~/.board (set BOARD_DIR to change) and appear when it starts.
  --json   print JSON instead of the ID (every command)
  -q       print nothing on success
  BOARD_URL  post over HTTP to a board on another machine instead

EXIT CODES
  0 ok    2 bad command or input (fix the command and retry)
  1 rejected by the board (unknown ID, wrong post kind, not an image,
    too large, port in use)
  3 BOARD_URL is set but that server is not reachable
  board log -- CMD exits with CMD's exit code.

HABITS
  Post when you start a task, find something, hit a failure, or finish.
  When you launch a subagent, tell it its name and to run `board help`,
  e.g. "Report progress with board -a lead/tests. Run board help first."
"""
