from django.db import migrations

def install(apps,schema_editor):
    vendor=schema_editor.connection.vendor
    if vendor=="sqlite":
        published=lambda ref: f"EXISTS(SELECT 1 FROM examinations_examschedule s JOIN examinations_exam e ON e.id=s.exam_id WHERE s.id={ref}.schedule_id AND e.status='published')"
        for op,refs in [("INSERT",["NEW"]),("UPDATE",["OLD","NEW"]),("DELETE",["OLD"])]:
            condition=" OR ".join(published(ref) for ref in refs)
            schema_editor.execute(f"""
                CREATE TRIGGER erp_mark_lock_{op.lower()} BEFORE {op} ON examinations_mark
                WHEN ({condition}) AND erp_mark_override()=0
                BEGIN SELECT RAISE(ABORT,'Published exam marks are locked'); END
            """)
    elif vendor=="postgresql":
        schema_editor.execute("""
        CREATE FUNCTION erp_guard_published_marks() RETURNS trigger AS $$
        DECLARE old_locked boolean:=false; new_locked boolean:=false;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                SELECT e.status='published' INTO old_locked FROM examinations_examschedule s
                JOIN examinations_exam e ON e.id=s.exam_id WHERE s.id=OLD.schedule_id;
            END IF;
            IF TG_OP <> 'DELETE' THEN
                SELECT e.status='published' INTO new_locked FROM examinations_examschedule s
                JOIN examinations_exam e ON e.id=s.exam_id WHERE s.id=NEW.schedule_id;
            END IF;
            IF (old_locked OR new_locked) AND current_setting('school_erp.mark_override',true) IS DISTINCT FROM 'on' THEN
                RAISE EXCEPTION 'Published exam marks are locked';
            END IF;
            IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER erp_mark_lock BEFORE INSERT OR UPDATE OR DELETE ON examinations_mark
        FOR EACH ROW EXECUTE FUNCTION erp_guard_published_marks();
        """)

def remove(apps,schema_editor):
    if schema_editor.connection.vendor=="sqlite":
        for op in ("insert","update","delete"):
            schema_editor.execute(f"DROP TRIGGER IF EXISTS erp_mark_lock_{op}")
    elif schema_editor.connection.vendor=="postgresql":
        schema_editor.execute("DROP TRIGGER IF EXISTS erp_mark_lock ON examinations_mark")
        schema_editor.execute("DROP FUNCTION IF EXISTS erp_guard_published_marks()")

class Migration(migrations.Migration):
    dependencies=[("examinations","0003_exam_grading_snapshot_exam_publication_version_and_more")]
    operations=[migrations.RunPython(install,remove)]
