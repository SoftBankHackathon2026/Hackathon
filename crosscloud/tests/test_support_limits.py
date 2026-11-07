import pytest

from crosscloud.mapping import support_limits

# sample-app packages/request-store/schema.sql 과 같은 형태.
_SUPPORTED_SCHEMA = """
-- Apply explicitly as a DB owner before starting the application.
CREATE TABLE IF NOT EXISTS public.migration_demo (
    request_id uuid PRIMARY KEY,
    run_id text NOT NULL,
    payload text NOT NULL DEFAULT 'REFERENCES are fine in strings',
    created_at timestamptz NOT NULL
);
"""

# pg_dump --schema-only 형태: PK 는 ALTER TABLE 로 따로 붙는다.
_PG_DUMP_SCHEMA = """
CREATE TABLE public.orders (
    id bigint NOT NULL,
    note text
);
CREATE SEQUENCE public.orders_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
CREATE FUNCTION public.touch() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN NEW.note := 'x'; RETURN NEW; END; $$;
ALTER SEQUENCE public.orders_id_seq OWNED BY public.orders.id;
ALTER TABLE ONLY public.orders ALTER COLUMN id
    SET DEFAULT nextval('public.orders_id_seq'::regclass);
ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);
"""


def _kinds(schema_sql):
    return {
        (limit.kind, limit.subject)
        for limit in support_limits.find_support_limits(schema_sql)
    }


def test_supported_schema_has_no_limits():
    assert support_limits.find_support_limits(_SUPPORTED_SCHEMA) == ()


def test_pg_dump_primary_key_and_plain_sequence_are_supported():
    assert support_limits.find_support_limits(_PG_DUMP_SCHEMA) == ()


@pytest.mark.parametrize(
    ("schema_sql", "expected"),
    [
        (
            "CREATE TABLE a (id int PRIMARY KEY, b int REFERENCES b(id));",
            ("foreign_key", "public.a"),
        ),
        (
            "CREATE TABLE a (id int PRIMARY KEY);"
            "ALTER TABLE ONLY public.a ADD CONSTRAINT f"
            " FOREIGN KEY (id) REFERENCES public.b(id);",
            ("foreign_key", "public.a"),
        ),
        (
            "CREATE TABLE a (id int PRIMARY KEY);"
            "CREATE TRIGGER t BEFORE INSERT ON public.a"
            " FOR EACH ROW EXECUTE FUNCTION f();",
            ("user_trigger", "public.a"),
        ),
        (
            "CREATE TABLE a (id int, at date, PRIMARY KEY (id, at))"
            " PARTITION BY RANGE (at);",
            ("partition_or_inheritance", "public.a"),
        ),
        (
            "CREATE TABLE a (id int PRIMARY KEY) INHERITS (base);",
            ("partition_or_inheritance", "public.a"),
        ),
        (
            "CREATE TABLE a (id int PRIMARY KEY);"
            "ALTER TABLE a ENABLE ROW LEVEL SECURITY;",
            ("row_level_security", "public.a"),
        ),
        (
            "CREATE TABLE a (id int PRIMARY KEY);"
            "CREATE POLICY p ON a USING (true);",
            ("row_level_security", "public.a"),
        ),
        (
            "CREATE TABLE a (id int PRIMARY KEY, tags text[]);",
            ("array_or_user_type", "public.a"),
        ),
        (
            "CREATE TYPE public.mood AS ENUM ('ok');",
            ("array_or_user_type", "public.mood"),
        ),
        (
            "CREATE TABLE a (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY);",
            ("generated_always", "public.a"),
        ),
        (
            "CREATE TABLE a (id int PRIMARY KEY);"
            "CREATE SEQUENCE s INCREMENT BY 2 OWNED BY a.id;",
            ("sequence_unsupported", "public.s"),
        ),
        (
            "CREATE TABLE a (id int PRIMARY KEY);"
            "CREATE SEQUENCE s CYCLE OWNED BY a.id;",
            ("sequence_unsupported", "public.s"),
        ),
        (
            'CREATE TABLE "Shop"."Items" (id int);',
            ("missing_primary_key", "shop.items"),
        ),
    ],
)
def test_detects_support_limit(schema_sql, expected):
    assert expected in _kinds(schema_sql)


@pytest.mark.parametrize(
    ("schema_sql", "expected"),
    [
        (
            "CREATE SEQUENCE ids;"
            "CREATE TABLE t (id bigint DEFAULT nextval('ids') PRIMARY KEY);",
            ("sequence_unsupported", "public.ids"),
        ),
        (
            "CREATE TABLE t (id bigint PRIMARY KEY);"
            "CREATE SEQUENCE s OWNED BY t.id;"
            "ALTER SEQUENCE s OWNED BY NONE;",
            ("sequence_unsupported", "public.s"),
        ),
        (
            "CREATE EXTENSION IF NOT EXISTS hstore;"
            "CREATE TABLE t (id int PRIMARY KEY);",
            ("extension", "hstore"),
        ),
        (
            "CREATE TABLE t (id int PRIMARY KEY, attrs hstore);",
            ("unsupported_column_type", "public.t"),
        ),
        (
            "CREATE TABLE t (id int PRIMARY KEY, ip inet);",
            ("unsupported_column_type", "public.t"),
        ),
        (
            "CREATE TABLE t (id int PRIMARY KEY, at time with time zone);",
            ("unsupported_column_type", "public.t"),
        ),
    ],
)
def test_detects_schema_outside_supported_scope(schema_sql, expected):
    assert expected in _kinds(schema_sql)


def test_supported_column_type_spellings_pass():
    schema_sql = """
    CREATE TABLE public.everything (
        "Id" bigserial PRIMARY KEY,
        a smallint, b integer, c int, d bigint, e serial,
        f character varying(20), g varchar(10), h char(3), i text,
        j boolean, k numeric(10, 2), l decimal, m real,
        n double precision, o float8, p date,
        q timestamp(3) without time zone, r timestamp with time zone,
        s timestamptz, t time, u uuid, v json, w jsonb, x bytea,
        CONSTRAINT everything_b_check CHECK (b > 0),
        UNIQUE (u)
    );
    """

    assert support_limits.find_support_limits(schema_sql) == ()


def test_schema_without_tables_cannot_be_judged():
    assert _kinds("-- empty\n") == {("no_tables", "schema")}
