# Predicción de Abandono (Churn) — Guía de Usuario

> Solución pre-fabricada de Dataiku, ya instalada en nuestra instancia local de Dataiku DSS.
> Ábrela en: **http://127.0.0.1:11000 → Projects → Churn Prediction**
> Clave del proyecto: `SOL_CHURN_PREDICTION` · Versión de la solución: **1.0.0** · Requiere **Dataiku ≥ 14.0** (la nuestra es 15.0.0).

---

## 1. ¿Qué es este proyecto?

Un **demo listo para usar de predicción de abandono (churn) a nivel de cliente y de producto**, creado por
Dataiku como una de sus "Industry Solutions" oficiales (línea de negocio: Marketing / Ventas / Cliente).
Usa datos sintéticos de **banca minorista**, pero el patrón (cliente × producto × tiempo) es genérico y se
puede adaptar a telecomunicaciones, seguros, suscripciones, etc.

La lógica de negocio:

- **Los clientes se van en silencio** — dejan de generar ingresos, dejan de usar productos o sus saldos caen
  a cero mucho antes de cerrar formalmente su cuenta.
- Cuando alguien ya abandonó, es demasiado tarde para retenerlo. Costó mucho captarlo; vale mucho más
  retenerlo.
- El proyecto examina el historial de **ingresos + saldos + productos contratados** por cliente y entrena un
  modelo que predice **quién tiene probabilidad de abandonar en los próximos 3 meses** — tiempo suficiente
  para actuar antes de perder al cliente.

¿A quién le sirve qué:

- **Gerentes de cuentas (Relationship Managers), responsables de producto, marketing, analistas
  de negocio:** los **dashboards** responden "¿qué está pasando?" y "¿quién está en riesgo?".
- **Arquitectos de datos / científicos de datos:** el Flow y la wiki integrada explican la lógica completa y
  cómo personalizarla.

## 2. ¿Qué produce?

| Entrega | Contenido |
|---|---|
| **Dashboard "Customer Level Churn Analysis"** | Clientes con riesgo de abandonar *toda* la relación en los próximos 3 meses · tasa de churn predicha vs. tendencia histórica · **ingresos en riesgo** · segmentación por perfil y *tier* del cliente · lista clasificada de clientes en riesgo |
| **Dashboard "Product Level Churn Analysis"** | Riesgo de abandono de cada producto individual (p. ej. el cliente cancela la tarjeta de crédito pero conserva la cuenta corriente) · predicciones a nivel cliente-producto-mes · **ingresos en riesgo por producto** · productos más propensos a abandono · lista de pares cliente-producto en riesgo |
| **Modelo guardado "Predict modeled_churn"** | Clasificación binaria que entrega por cada fila cliente-producto-mes una probabilidad de churn de 0 a 1, con la que se ordena el riesgo y se priorizan las acciones de retención |

## 3. Qué hay dentro — guía detallada del Flow

El Flow está organizado en **9 zonas modulares** de izquierda a derecha. Cada zona es autocontenida: se
puede reutilizar, sustituir o adaptar sin tocar las demás.

### Zona 1 — Data Ingestion (Ingesta de datos)
Recoge las 5 tablas de entrada del caso (~16 meses de historia bancaria sintética):

| Dataset | Contenido |
|---|---|
| `customers_data` | Atributos demográficos y estáticos (id, género, región, ingresos, tier, fecha de nacimiento, segmento…) |
| `product_holdings_data` | Qué productos tiene cada cliente y cuándo los contrató/cerró (producto, tipo, fechas de inicio y fin) |
| `revenues_data` | Ingresos mensuales por cliente-producto |
| `balances_data` | Saldos mensuales por cliente-producto |
| `additional_information_data` | Datos opcionales (actividad en cajeros, servicios digitales, interacciones…) |

Cada tabla se **copia (sync)** a una versión "curada" dentro del proyecto (p. ej. `customers_info`). Las
tablas de ingresos, saldos y productos solo llevan columnas obligatorias; la de clientes y la de información
adicional admiten columnas extra para enriquecer el modelo.

### Zona 2 — Customer-Product Mapping (Mapeo cliente-producto)
- Extrae la **lista de meses únicos** de los datos de ingresos para construir un eje temporal completo
  (así todos los pares cliente-producto tienen el mismo número de meses, sin huecos).
- **Cruza (join)** el padrón de clientes con la cartera de productos: resultado = una vista detallada de
  "qué producto tiene cada cliente y durante qué fechas", base para todo lo que sigue.

### Zona 3 — Combined Data (Datos combinados)
- Une la tabla cliente-producto-fecha con los **ingresos y saldos mensuales** correspondientes (y la
  información adicional si hay), quedando una sola tabla de comportamiento: una fila por
  cliente–producto–mes.

### Zona 4 — Data Preparation & Feature Generation (Limpieza y creación de características)
- Limpieza básica y **filtrado para conservar solo filas en las que el producto está financieramente
  activo** (en vigor en esa fecha).
- Recetas **de ventana (Window)** que calculan, por cada par cliente-producto:
  - ingresos totales y número de productos;
  - la media/suma de **ingresos y saldos de los últimos 6 y 12 meses**;
- Recetas específicas de **lags** ("ingresos de hace 1–3 meses") — servirán para *definir* el churn real;
- Recetas de **leads** ("ingresos en los próximos 1–3 meses") — servirán para *etiquetar* la variable
  objetivo del modelo.

### Zona 5 — Churn Definition (Definición de abandono: el dato real)
Aquí **no hay predicción**: es una regla de negocio *hacia atrás* que dice cuándo un cliente ya abandonó
un producto. Una fila cliente-producto-mes se etiqueta como **churn real** si:

- lleva **3 meses consecutivos con ingresos = 0** (`inactivity_churned`), **o**
- el producto está **oficialmente cerrado** en esa fecha (`product_end_churned`).

Esta zona también hace dos cosas clave:

- **Asigna el flag `row_flag`** (`for_modeling` vs. `for_inference` / scoring) según una **fecha de corte
  fija** que tú eliges: filas con etiqueta conocida → entrenamiento; filas posteriores, cuyo futuro aún no
  conocemos → scoring.
- Identifica, por cada relación cliente-producto, **la primera fecha en que aparece el churn real** y
  **descarta todas las filas posteriores**: así el modelo nunca aprende de datos "post-abandono"
  (anti-leakage).

### Zona 6 — Model Input Data Preparation (Preparación de los datos de entrada del modelo)
Construye la tabla supervisada de entrenamiento. Antes de etiquetar, genera **características de
cross-product** (qué otros productos tiene el cliente, p. ej. préstamos) para que el modelo pueda usar la
influencia del resto del portafolio. Luego asigna la **etiqueta futura** que sí usa el modelo:

> Etiqueta **"modeled_churn"** = 1 si los ingresos serán **cero en los próximos 3 meses** (es decir,
> el cliente se va a desvincular *después* de esa fila). Es la variable objetivo; solo se calcula en las
> filas `for_modeling`, porque para las posteriores aún no conocemos el futuro.

Igual que en la zona 5, tras la primera fecha de churn futuro se descartan las filas posteriores
(no-leakage).

### Zona 7 — Churn Prediction (El modelo)
El problema de churn en banca está **muy desbalanceado** (la inmensa mayoría de las filas son "no churn"),
y los clasificadores tienden a votar por la clase mayoritaria. Por eso:

1. Un recipe **Split** separa las filas con label conocido del resto (scoring).
2. Otro Split divide el set modelable en: `train_data_1` (todas las filas churn) y `train_data_0`
   (no-churn).
3. Un **Filter/Sampling** selecciona aleatoriamente ~100 000 filas de las no-churn (undersampling).
4. Un **Stack** junta las no-churn muestreadas con **todas** las churn, produciendo
   `training_data_prepared`, la tabla balanceada con la que se entrena.

El modelo de **clasificación binaria** se entrena sobre `training_data_prepared` con las características
de comportamiento, transacciones y portafolio. Devuelve por cada fila:

- la **predicción binaria** (churn: 0/1) y
- la **probabilidad de churn** (0.0–1.0) para ordenar riesgos.

Optimización:

- **ROC AUC** como métrica principal (qué tan bien distingue abandonadores de no abandonadores).
- **Matriz de costes** para fijar el **umbral de corte**: en banca, pasar por alto a un cliente que se
  iba (falso negativo) cuesta mucho más que avisar por error a un cliente fiel (falso positivo).

### Zona 8 — Predicted Churn Visualisation (Alimentando el dashboard predictivo)
Convierte la salida del modelo en datasets listos para el dashboard. El modelo se aplica (recipe
*Score*) a la tabla `inference_dataset` (filas cuyo futuro aún desconocemos) generando
`inference_dataset_scored`; después se preparan (`inference_scored_prepared`), filtran y agregan a nivel
cliente (`pred_churned_products`, `At_risk_customers`, `At_Risk_products`…) para poblar los dashboards.

### Zona 9 — Historical Churn Visualisation (Tendencias históricas)
Aplica la misma lógica al **pasado** usando las etiquetas de churn real de la zona 5, para generar
insights de "qué pasó antes" (tendencia histórica de churn, por producto, por tier, por cliente). Sirve de
referencia antes de actuar sobre las predicciones.

## 4. Paso a paso — primer uso (demo con los datos de muestra)

1. **Abrir el proyecto.** Entra a DSS → Projects → **Churn Prediction**.
2. **Entrenar el modelo una vez.** El modelo importado viene *vacío* por diseño (el bundle incluye la
   configuración pero no las versiones entrenadas).
   - Abre el **Flow** y busca el nodo del modelo **"Predict modeled_churn (binary)"** → **Open**, o abre
     directamente el análisis de Lab **"Binary Classification for Churn"**.
   - Pulsa **Train** (arriba a la derecha). Con los datos de muestra tarda unos minutos.
   - En la sesión de entrenamiento, elige el mejor experimento y pulsa **Deploy** → se materializa como
     versión 1 del modelo guardado, que queda activa.
3. **Ejecutar el scoring y poblar los dashboards.** En el Flow:
   - Recipe **score_inference_dataset** (produce `inference_dataset_scored`) → **Run**.
   - Y luego la rama que sigue: `inference_scored_prepared` y los datasets de dashboards
     (`pred_churned_products`, `At_risk_customers`, `At_Risk_products`…). La forma más fácil: abrir un
     dataset de dashboard y pulsar **Build**, dejando que Dataiku ejecute todos los pasos que faltan.
4. **Explorar los dashboards.**
   - **Customer Level Churn Analysis**: tarjetas superiores con estimados de abandono completo, tasa de
     churn predicha vs. histórica, ingresos en riesgo; abajo, tabla de clientes en riesgo y churn por tier.
   - **Product Level Churn Analysis**: misma estructura pero por producto.
5. **(Opcional) Leer la Wiki del proyecto** — está integrada en el propio proyecto ("Learn more about the
   Solution" en la página del proyecto) y describe cada zona con capturas.

## 5. Paso a paso — usarlo con **tus propios datos**

1. **Prepara tus tablas de entrada** respetando el esquema esperado (mismos nombres de columna, tipos y
   granularidad: una fila por cliente–producto–mes). Verifica que `revenues_data`, `balances_data` y
   `product_holdings_data` cubran el **mismo periodo** y tengan la **misma frecuencia** (mensual). Los
   formatos exactos figuran en la Wiki del proyecto, artículo "Data Model".
2. **Remapea las 5 tablas de entrada** del Flow para apuntar a tus fuentes reales (sustituye los datasets
   `*_data`).
3. **Ajusta la definición de churn** (zona 5) y la **fecha de corte** a tu caso: quizá en tu negocio
   el abandono no es "0 ingresos durante 3 meses" sino, por ejemplo, saldo nulo, cuenta no usada, etc.
4. **Re-entrena el modelo** sobre tu histórico y vuelve a ejecutar el scoring para poblar los dashboards.

> **Agnóstico respecto al negocio:** el pipeline no cambia, lo único que cambia es el mapeo de datos. Por
> ejemplo, en **telecom**: producto = plan/servicio, ingresos = facturación/ARPU; en **seguros**:
> producto = póliza, ingresos = primas; en **suscripciones** (OTT/SaaS): producto = suscripción,
> ingresos = cuota, columna adicional = logins/uso.

## 6. Qué esperar / advertencias

- **Los datos del demo son sintéticos.** Los números de los dashboards son ilustrativos. (El sample: ~22 000
  clientes, 10 productos en 4 categorías, >600 000 filas de ingresos, >400 000 de saldos, ~16 meses.)
- **Hay que entrenar el modelo una vez** antes de que funcione el scoring (`exportSavedModels: false` en el
  bundle del fabricante); es intencional para re-entrenar con tus datos.
- Los dashboards **históricos** ya tienen contenido tras la build inicial; la parte **predictiva** solo se
  puebla después de entrenar el modelo y ejecutar el scoring.
- La granularidad del modelo es **cliente–producto–mes**; los dashboards agregan eso a nivel cliente.
- Es una **plantilla de partida**, no un producto acabado: conviene iterar la definición de abandono y las
  características a tu negocio.
- **Sin código y sin entorno Python adicional**: todo el pipeline son recetas visuales (no se necesita
  code-env) y está probado con almacenamiento en **Filesystem** y **Snowflake**.

## 7. Enlaces útiles dentro del proyecto

- Dashboards: **Customer Level Churn Analysis** (`/dashboards/WakSLY7`) y **Product Level Churn Analysis**
  (`/dashboards/5TpFFXE`)
- Análisis de Lab: **Binary Classification for Churn** (análisis `bwPvyDGk` · ML task `daynzZKQ`)
- Wiki del proyecto: artículos 1–21 en el propio proyecto (botón **Wiki**)
- Documentación oficial:
  https://knowledge.dataiku.com/latest/solutions/financial-services/solution-churn-prediction.html

---

*Instalado en esta instancia DSS 15.0.0 mediante la Public API (`projectsFromBundle` + preload/activate).
Inventario: 57 datasets, 51 recipes, 1 análisis de Visual ML (contiene el modelo, que requiere un primer
entrenamiento), 2 dashboards, 16 insights. Versión de la solución: 1.0.0 (2025-08).*
