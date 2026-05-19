"""
Seed the database with test users and loops.
Run: python -m scripts.seed
"""
import asyncio
import uuid
from decimal import Decimal
from sqlalchemy import select
from app.database import AsyncSessionLocal, engine, Base
from app.models.user import User, UserRole
from app.models.loop import Loop, Genre, TempoFeel
from app.models.drone_pad import DronePadCategory
from app.services.auth_service import hash_password


async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        # Skip users that already exist
        existing_emails = set(
            await db.scalars(select(User.email))
        )

        users_to_add = []
        if "admin@litmusic.app" not in existing_emails:
            users_to_add.append(User(
                id=uuid.uuid4(),
                email="admin@kida.app",
                password_hash=await hash_password("admin1234"),
                full_name="LitMusic Admin",
                role=UserRole.admin,
            ))
        if "producer@litmusic.app" not in existing_emails:
            users_to_add.append(User(
                id=uuid.uuid4(),
                email="producer@kida.app",
                password_hash=await hash_password("producer1234"),
                full_name="Test Producer",
                role=UserRole.producer,
            ))
        if "user@litmusic.app" not in existing_emails:
            users_to_add.append(User(
                id=uuid.uuid4(),
                email="user@kida.app",
                password_hash=await hash_password("user1234"),
                full_name="Test User",
                role=UserRole.user,
            ))

        if users_to_add:
            db.add_all(users_to_add)
            await db.flush()

        # Resolve admin id for loop FK
        admin = await db.scalar(select(User).where(User.email == "admin@litmusic.app"))

        # Skip loop if already seeded
        existing_loop = await db.scalar(
            select(Loop).where(Loop.slug == "afro-vibes-loop-1-seed")
        )
        if not existing_loop:
            loop = Loop(
                id=uuid.uuid4(),
                title="Afro Vibes Loop 1",
                slug="afro-vibes-loop-1-seed",
                genre=Genre.afrobeat,
                bpm=100,
                key="A minor",
                duration=32,
                tempo_feel=TempoFeel.mid,
                tags=["afrobeat", "drums", "vibes"],
                price=Decimal("4.99"),
                is_free=True,
                is_paid=False,
                created_by=admin.id,
            )
            db.add(loop)

        # Seed drone pad categories
        existing_category_names = set(
            await db.scalars(select(DronePadCategory.name))
        )
        categories = [
            ("Cinematic", "Sweeping, orchestral drone pads for film and trailer scoring"),
            ("Ambient", "Atmospheric textures for meditation, lo-fi, and background music"),
            ("Worship", "Warm, ethereal pads for gospel and contemporary worship"),
            ("Dark / Tension", "Ominous, dissonant drones for suspense and horror"),
            ("Uplifting", "Bright, soaring pads for motivational and inspirational content"),
            ("Electronic", "Synthesized drone textures for EDM, synthwave, and pop"),
            ("World / Ethnic", "Culturally-inspired drone layers from global musical traditions"),
            ("Nature / Organic", "Acoustic and nature-derived drones with earthy, raw character"),
            ("Bright", "crisp, airy drone pads with a clean, open quality"),
            ("Shimmer", "Glistening, high-frequency textures with a sparkling, evolving character"),
            ("Orchestral", "Full string, brass, and woodwind drone layers for a rich ensemble sound"),
            ("Ethereal", "Delicate, otherworldly pads with a floating, transcendent quality"),
        ]
        categories_to_add = [
            DronePadCategory(
                id=uuid.uuid4(),
                name=name,
                description=description,
                created_by=admin.id,
            )
            for name, description in categories
            if name not in existing_category_names
        ]
        if categories_to_add:
            db.add_all(categories_to_add)

        await db.commit()
        print("Seed complete.")
        print("  Admin:    admin@litmusic.app / admin1234")
        print("  Producer: producer@litmusic.app / producer1234")
        print("  User:     user@litmusic.app / user1234")
        print(f"  Drone pad categories seeded: {len(categories_to_add)}")


if __name__ == "__main__":
    asyncio.run(seed())
