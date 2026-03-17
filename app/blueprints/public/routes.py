from datetime import date

from flask import Blueprint, Response, render_template, url_for
from ...extensions import db
from ...models import Master, Banner, Review, Work, WorkCategory, Competency


bp = Blueprint("public", __name__)


@bp.get("/")
def index():
    banners = db.session.execute(
        db.select(Banner).where(Banner.is_active == True).order_by(Banner.order)
    ).scalars().all()
    
    reviews = db.session.execute(
        db.select(Review).where(Review.is_published == True).order_by(Review.created_at.desc()).limit(10)
    ).scalars().all()

    featured_works = db.session.execute(
        db.select(Work).where(Work.is_active == True).order_by(Work.base_price.is_(None), Work.base_price, Work.title).limit(6)
    ).scalars().all()

    active_masters_count = db.session.execute(
        db.select(db.func.count(Master.id)).where(Master.is_active == True)
    ).scalar_one()
    active_works_count = db.session.execute(
        db.select(db.func.count(Work.id)).where(Work.is_active == True)
    ).scalar_one()
    reviews_count = db.session.execute(
        db.select(db.func.count(Review.id)).where(Review.is_published == True)
    ).scalar_one()
    avg_rating = db.session.execute(
        db.select(db.func.avg(Review.rating)).where(Review.is_published == True)
    ).scalar()

    return render_template(
        "public/index.html",
        banners=banners,
        reviews=reviews,
        featured_works=featured_works,
        active_masters_count=active_masters_count,
        active_works_count=active_works_count,
        reviews_count=reviews_count,
        avg_rating=float(avg_rating or 0),
    )


@bp.get("/services")
def services():
    works = db.session.execute(
        db.select(Work).join(WorkCategory, Work.category_id == WorkCategory.id).where(Work.is_active == True).order_by(WorkCategory.title, Work.title)
    ).scalars().all()
    categories_count = db.session.execute(
        db.select(db.func.count(WorkCategory.id))
    ).scalar_one()
    return render_template("public/services.html", works=works, categories_count=categories_count)


@bp.get("/masters")
def masters():
    all_masters = db.session.execute(
        db.select(Master).order_by(Master.name)
    ).scalars().all()
    active_masters = sum(1 for master in all_masters if master.is_active)
    competencies_count = db.session.execute(
        db.select(db.func.count(Competency.id))
    ).scalar_one()
    return render_template(
        "public/masters.html",
        masters=all_masters,
        active_masters=active_masters,
        competencies_count=competencies_count,
    )


@bp.get("/contacts")
def contacts():
    return render_template("public/contacts.html")


@bp.get("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /auth/",
        "Disallow: /cabinet/",
        f"Sitemap: {url_for('public.sitemap_xml', _external=True)}",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@bp.get("/sitemap.xml")
def sitemap_xml():
    today = date.today().isoformat()
    urls = [
        url_for("public.index", _external=True),
        url_for("public.services", _external=True),
        url_for("public.masters", _external=True),
        url_for("public.contacts", _external=True),
    ]
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc in urls:
        body.append("<url>")
        body.append(f"<loc>{loc}</loc>")
        body.append(f"<lastmod>{today}</lastmod>")
        body.append("</url>")
    body.append("</urlset>")
    return Response("\n".join(body), mimetype="application/xml")
