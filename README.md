# MFL Soil Data API

REST API para gestionar datos de suelos de múltiples proyectos en América Latina, construida con FastAPI y el ORM mflsoildata_orm.

## 🚀 Características

- ✅ API RESTful de alto rendimiento con FastAPI
- 📊 Gestión de proyectos, sitios, muestras y mediciones de suelo
- 🗺️ Soporte para georreferenciación con PostGIS
- 🔄 ORM mflsoildata_orm para interacción con la base de datos
- 📚 Documentación automática con Swagger & ReDoc
- 🧩 Estructura modular y escalable
- ⚙️ Sin autenticación (próximamente Keycloak)

## 📋 Requisitos

- Python > 3.10
- PostgreSQL + PostGIS
- pip o virtualenv

## 🔐 Configuración del Ambiente

Crea un archivo `.env` en la raíz del proyecto con la siguiente estructura:

```env
DATABASE_URL=postgresql://usuario:contraseña@localhost:5432/soil_platform
```

## 🛠️ Instalación

1. **Clona el repositorio**
```bash
git clone https://github.com/CIAT-DAPA/mflsoildata_api.git
cd mflsoildata_api
```

2. **Crea un ambiente virtual** (opcional pero recomendado)
```bash
python -m venv venv

# En Windows
venv\Scripts\activate

# En Linux/Mac
source venv/bin/activate
```

3. **Instala las dependencias**
```bash
pip install -r requirements.txt
```

## 🚀 Ejecutar la API

Desde el directorio `src`:

```bash
cd src
uvicorn main:app --reload
```

La API estará disponible en `http://localhost:8000`

### Documentación Interactiva

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

## 📚 Estructura del Proyecto

```
mflsoildata_api/
├── src/
│   ├── main.py                 # Punto de entrada de la API
│   ├── routes/                 # Rutas/endpoints
│   │   ├── projects.py
│   │   ├── sites.py
│   │   ├── samples.py
│   │   └── measurements.py
│   ├── schemas/                # Esquemas Pydantic
│   │   ├── projects.py
│   │   ├── sites.py
│   │   ├── samples.py
│   │   ├── sampling_events.py
│   │   ├── variables.py
│   │   └── measurements.py
│   ├── dependencies/           # Dependencias comunes
│   └── tests/                  # Pruebas unitarias
├── .env                        # Variables de ambiente
├── requirements.txt            # Dependencias del proyecto
├── .gitignore
└── README.md
```

## 📖 Endpoints Principales

### Projects
- `GET /projects/` - Obtener todos los proyectos
- `GET /projects/{project_id}` - Obtener un proyecto específico
- `POST /projects/` - Crear un nuevo proyecto
- `PUT /projects/{project_id}` - Actualizar un proyecto
- `DELETE /projects/{project_id}` - Eliminar un proyecto

### Sites
- `GET /sites/` - Obtener sitios (requiere `project_id`)
- `GET /sites/{site_id}` - Obtener un sitio específico
- `POST /sites/` - Crear un nuevo sitio
- `PUT /sites/{site_id}` - Actualizar un sitio

### Samples
- `GET /samples/` - Obtener muestras (con filtros opcionales)
- `GET /samples/{sample_id}` - Obtener una muestra específica
- `POST /samples/` - Crear una nueva muestra
- `PUT /samples/{sample_id}` - Actualizar una muestra

### Measurements
- `GET /measurements/` - Obtener mediciones (con filtros opcionales)
- `GET /measurements/{measurement_id}` - Obtener una medición específica
- `POST /measurements/` - Crear una nueva medición

### Health
- `GET /health` - Verificar el estado de la API

## 🔄 Flujo de Datos Recomendado

1. Crear un **Project**
2. Crear **Sites** asociados al proyecto con coordenadas (latitude, longitude)
3. Crear **Sampling Events** para registrar visitas/campañas
4. Crear **Samples** (muestras de suelo, hoja, grano) asociadas a eventos
5. Crear **Measurements** con variables (pH, SOC, etc.) para cada sample

## 🗄️ Base de Datos

El esquema está optimizado para:
- EAV (Entity-Attribute-Value) para atributos flexibles
- Soporte de múltiples matrices (SOIL, LEAF, GRAIN)
- Trazabilidad completa (timestamps, fuentes de datos)
- Georreferenciación con PostGIS

Ver `DB_CONTEXT_soil_platform_v6.md` para detalles completos del modelo de datos.

## 🧪 Pruebas

Ejecutar pruebas:

```bash
cd src
pytest tests/
```

## 📝 Notas Importantes

- Actualmente **no hay autenticación**. Todos los endpoints son públicos.
- La API usa `mflsoildata_orm` para la interacción con la BD.
- Se recomienda usar la estructura existente al agregar nuevos endpoints.
- Siempre normalizar códigos (uppercase, sin tildes, espacios a `_`).

## 🔗 Referencias

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [mflsoildata_orm](https://github.com/Dan3s/mflsoildata_orm)
- [aclimate_v3_webapi](https://github.com/CIAT-DAPA/aclimate_v3_webapi) (estructura base)

## 📄 Licencia

Por definir

## 👥 Contacto

Para preguntas o sugerencias, contacta al equipo de desarrollo.
